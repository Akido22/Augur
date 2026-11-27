"""RiskContextBuilder (чтение БД), market_snapshot (адаптер), return_zscore, миграция 0004."""

import sqlite3
from decimal import Decimal

import pytest

from augur.adapters.base import AccountState, InstrumentSpec, MarketStatus
from augur.config.settings import TradingMode
from augur.data.models import Tick
from augur.risk.context import RiskContextBuilder, market_snapshot, return_zscore
from augur.risk.gate import intent_key
from tests._bars import make_bars
from tests._risk import INSTRUMENT, make_signal


def _builder(db: sqlite3.Connection, mode: TradingMode = TradingMode.PAPER) -> RiskContextBuilder:
    return RiskContextBuilder(db, configured_mode=mode)


def test_migration_0004_tables(db: sqlite3.Connection) -> None:
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"positions", "portfolio_snapshots"} <= tables
    db.execute(
        "INSERT INTO positions(venue, symbol, mode, qty, avg_price, updated_ts) "
        "VALUES ('paper', 'BTC/USDT', 'PAPER', '1', '100', 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):  # PK (venue, symbol, mode)
        db.execute(
            "INSERT INTO positions(venue, symbol, mode, qty, avg_price, updated_ts) "
            "VALUES ('paper', 'BTC/USDT', 'PAPER', '2', '100', 2)"
        )
    with pytest.raises(sqlite3.IntegrityError):  # STRICT: TEXT-колонка не принимает blob
        db.execute(
            "INSERT INTO portfolio_snapshots(ts, mode, equity, cash, day_pnl, peak_equity, "
            "drawdown_pct, positions_json) VALUES (1, 'PAPER', x'00', '1', '0', '1', '0', '{}')"
        )


def test_mode_defaults_to_config_and_is_capped(db: sqlite3.Connection) -> None:
    assert _builder(db).current_mode() == TradingMode.PAPER
    db.execute(
        "INSERT INTO mode_state(id, mode, changed_ts, changed_by) VALUES (1, 'FULL_AUTO', 1, 't')"
    )
    assert _builder(db).current_mode() == TradingMode.PAPER  # не выше конфига (ARCH §7.1)
    db.execute("UPDATE mode_state SET mode='SIGNAL_ONLY'")
    assert _builder(db).current_mode() == TradingMode.SIGNAL_ONLY
    db.execute("UPDATE mode_state SET mode='garbage'")
    assert _builder(db).current_mode() == TradingMode.ANALYSIS_ONLY


def test_cb_state(db: sqlite3.Connection) -> None:
    assert _builder(db).cb_state() == "ARMED"
    db.execute("INSERT INTO cb_state(id, state) VALUES (1, 'TRIPPED')")
    assert _builder(db).cb_state() == "TRIPPED"
    db.execute("UPDATE cb_state SET state='KILLED'")
    assert _builder(db).cb_state() == "KILLED"


def test_positions_filtered_by_mode(db: sqlite3.Connection) -> None:
    db.executemany(
        "INSERT INTO positions(venue, symbol, mode, qty, avg_price, updated_ts) "
        "VALUES (?,?,?,?,?,1)",
        [
            ("paper", "BTC/USDT", "PAPER", "-1.5", "100"),
            ("ccxt", "ETH/USDT", "MICRO_LIVE", "3", "10"),
        ],
    )
    assert _builder(db).positions(TradingMode.PAPER) == {"BTC/USDT": Decimal("-1.5")}


def test_orders_unknown_and_active_intents(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO signals(id, kind, symbol, action, strength, source, config_hash, "
        "code_version, created_ts) VALUES ('S1', 'final', 'BTC/USDT', 'BUY', 0.5, 'classic', "
        "'c', 'v', 1)"
    )
    rows = [
        ("O1", "c1", "UNKNOWN", "BUY"),
        ("O2", "c2", "SUBMITTED", "SELL"),
        ("O3", "c3", "FILLED", "BUY"),
    ]
    for oid, coid, state, side in rows:
        db.execute(
            "INSERT INTO orders(id, client_order_id, signal_id, risk_verdict_id, venue, symbol, "
            "side, type, qty, state, mode, created_ts, updated_ts) "
            "VALUES (?, ?, 'S1', 'V', 'paper', 'BTC/USDT', ?, 'MARKET', '1', ?, 'PAPER', 1, 1)",
            (oid, coid, side, state),
        )
    b = _builder(db)
    assert b.unknown_order_symbols() == frozenset({"BTC/USDT"})
    assert b.active_intents() == frozenset({intent_key("S1", "BUY"), intent_key("S1", "SELL")})


def test_build_without_snapshot_leaves_pnl_unknown(db: sqlite3.Connection) -> None:
    from augur.risk.context import MarketSnapshot
    from tests._risk import NOW

    ctx = _builder(db).build(make_signal(), MarketSnapshot(), now=NOW)
    assert ctx.day_pnl is None and ctx.equity_start_of_day is None
    assert ctx.mode == TradingMode.PAPER and ctx.cb_state == "ARMED" and ctx.price is None


def test_return_zscore() -> None:
    assert return_zscore(make_bars([100, 101, 102])) is None  # мало доходностей
    calm = make_bars([100, 101, 100, 101, 100, 101, 100])
    spike = make_bars([100, 101, 100, 101, 100, 101, 150])
    z_calm, z_spike = return_zscore(calm), return_zscore(spike)
    assert z_calm is not None and z_spike is not None
    assert abs(z_spike) > Decimal(4) > abs(z_calm)


class _FakeAdapter:
    venue = "fake"

    async def market_status(self, symbol: str) -> MarketStatus:
        return MarketStatus.OPEN

    async def get_latest(self, symbol: str) -> Tick:
        return Tick(
            symbol=symbol,
            venue="fake",
            ts=1,
            bid=Decimal("99.9"),
            ask=Decimal("100.1"),
            volume=5000.0,
        )

    async def get_instrument(self, symbol: str) -> InstrumentSpec:
        return INSTRUMENT

    async def get_account_state(self) -> AccountState:
        return AccountState(
            equity=Decimal("1000"),
            cash=Decimal("900"),
            margin_used=Decimal("0"),
            currency="USDT",
            ts=1,
        )


async def test_market_snapshot_from_adapter() -> None:
    snap = await market_snapshot(_FakeAdapter(), "BTC/USDT")  # type: ignore[arg-type]
    assert snap.price == Decimal("100.0")
    assert snap.spread_bps == Decimal("20")  # 0.2 / 100 × 10000
    assert snap.liquidity_quote == Decimal("500000.0")
    assert snap.equity == Decimal("1000") and snap.cash == Decimal("900")
    assert snap.volatility_zscore is None  # бары не переданы → R7 fail-closed
