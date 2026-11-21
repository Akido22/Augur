"""OhlcvLoader: UPSERT идемпотентен, незакрытые и некорректные бары не попадают в ohlcv."""

import sqlite3

from augur.adapters import PaperAdapter
from augur.data.loader import OhlcvLoader
from augur.data.models import Bar, Tick, timeframe_ms

H = timeframe_ms("1h")
T0 = 1_795_000_000_000 // H * H
NOW = T0 + 100 * H


def _rows(db: sqlite3.Connection) -> list[tuple[object, ...]]:
    return db.execute(
        "SELECT venue, symbol, timeframe, ts_open, open, high, low, close, volume FROM ohlcv "
        "ORDER BY ts_open"
    ).fetchall()


async def test_reload_is_idempotent(db: sqlite3.Connection) -> None:
    loader = OhlcvLoader(db, PaperAdapter(clock_ms=NOW))
    first = await loader.load("BTC/USDT", "1h", T0, T0 + 24 * H)
    rows_after_first = _rows(db)
    second = await loader.load("BTC/USDT", "1h", T0, T0 + 24 * H)

    assert (first.fetched, first.inserted, first.updated) == (24, 24, 0)
    assert (second.fetched, second.inserted, second.updated) == (24, 0, 0)
    assert _rows(db) == rows_after_first
    assert len(rows_after_first) == 24


async def test_overlapping_ranges_do_not_duplicate(db: sqlite3.Connection) -> None:
    loader = OhlcvLoader(db, PaperAdapter(clock_ms=NOW))
    await loader.load("BTC/USDT", "1h", T0, T0 + 10 * H)
    res = await loader.load("BTC/USDT", "1h", T0 + 5 * H, T0 + 15 * H)
    assert res.inserted == 5
    count, distinct = db.execute("SELECT COUNT(*), COUNT(DISTINCT ts_open) FROM ohlcv").fetchone()
    assert count == distinct == 15


async def test_open_bar_is_not_stored(db: sqlite3.Connection) -> None:
    loader = OhlcvLoader(db, PaperAdapter(clock_ms=NOW))
    res = await loader.load("BTC/USDT", "1h", NOW - 2 * H, NOW + H)
    assert res.skipped_open == 1
    assert [r[3] for r in _rows(db)] == [NOW - 2 * H, NOW - H]


class _ScriptedAdapter(PaperAdapter):
    """PaperAdapter, отдающий заранее заданные бары."""

    def __init__(self, bars: list[Bar]) -> None:
        super().__init__(clock_ms=NOW)
        self.bars = bars

    async def get_historical(self, symbol: str, timeframe: str, start: int, end: int) -> list[Bar]:
        return self.bars

    async def get_latest(self, symbol: str) -> Tick:  # pragma: no cover — не используется
        raise NotImplementedError


def _bar(ts: int, close: float = 101.0, high: float = 102.0) -> Bar:
    return Bar(
        symbol="EURUSD",
        venue="mt5",
        timeframe="1h",
        ts_open=ts,
        open=100.0,
        high=high,
        low=99.0,
        close=close,
        volume=10.0,
    )


async def test_revised_bar_is_updated_not_duplicated(db: sqlite3.Connection) -> None:
    await OhlcvLoader(db, _ScriptedAdapter([_bar(T0)])).load("EURUSD", "1h", T0, T0 + H)
    res = await OhlcvLoader(db, _ScriptedAdapter([_bar(T0, close=101.5)])).load(
        "EURUSD", "1h", T0, T0 + H
    )
    assert (res.inserted, res.updated) == (0, 1)
    assert [r[7] for r in _rows(db)] == [101.5]


async def test_invalid_bar_rejected_and_audited(db: sqlite3.Connection) -> None:
    bad = _bar(T0 + H, high=50.0)  # high < max(open, close)
    res = await OhlcvLoader(db, _ScriptedAdapter([_bar(T0), bad])).load(
        "EURUSD", "1h", T0, T0 + 2 * H
    )
    assert (res.inserted, res.rejected) == (1, 1)
    event, ref_id, prev_hash, row_hash = db.execute(
        "SELECT event, ref_id, prev_hash, row_hash FROM audit_log"
    ).fetchone()
    assert event == "DATA_ANOMALY"
    assert ref_id == f"mt5|EURUSD|1h|{T0 + H}"
    assert prev_hash is None and len(row_hash) == 64
