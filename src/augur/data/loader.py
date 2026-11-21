"""Загрузка истории OHLCV через адаптер с идемпотентным UPSERT в SQLite.

- В БД попадают только закрытые бары (незакрытый бар — защита от look-ahead, ARCH §2.3).
- Некорректные бары отбрасываются с записью `DATA_ANOMALY` в `audit_log`.
- Повторная загрузка того же диапазона не создаёт дублей (PK + ON CONFLICT);
  строка перезаписывается, только если биржа изменила значения бара.
"""

import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from augur.data.models import Bar, bar_anomalies
from augur.db.audit import append_audit

if TYPE_CHECKING:  # без рантайм-импорта: adapters.base сам импортирует augur.data.models
    from augur.adapters.base import MarketAdapter

_UPSERT = """
INSERT INTO ohlcv(venue, symbol, timeframe, ts_open, open, high, low, close, volume, ingested_ts)
VALUES (:venue, :symbol, :timeframe, :ts_open, :open, :high, :low, :close, :volume, :ingested_ts)
ON CONFLICT(venue, symbol, timeframe, ts_open) DO UPDATE SET
    open = excluded.open, high = excluded.high, low = excluded.low,
    close = excluded.close, volume = excluded.volume, ingested_ts = excluded.ingested_ts
WHERE (ohlcv.open, ohlcv.high, ohlcv.low, ohlcv.close, ohlcv.volume)
   IS NOT (excluded.open, excluded.high, excluded.low, excluded.close, excluded.volume)
"""


@dataclass(frozen=True)
class LoadResult:
    """Итог загрузки диапазона."""

    fetched: int  # получено от адаптера
    inserted: int  # новых строк в ohlcv
    updated: int  # перезаписанных строк (биржа изменила бар)
    skipped_open: int  # незакрытые бары, не сохранены
    rejected: int  # некорректные бары (DATA_ANOMALY)


class OhlcvLoader:
    """Загрузчик OHLCV: адаптер → валидация → UPSERT в `ohlcv`."""

    def __init__(self, conn: sqlite3.Connection, adapter: "MarketAdapter") -> None:
        self._conn = conn
        self._adapter = adapter

    async def load(self, symbol: str, timeframe: str, start: int, end: int) -> LoadResult:
        """Загружает бары `start <= ts_open < end` (Unix ms UTC) одной транзакцией."""
        bars = await self._adapter.get_historical(symbol, timeframe, start, end)
        now = int(time.time() * 1000)
        good: list[Bar] = []
        skipped_open = 0
        anomalies: list[tuple[Bar, list[str]]] = []
        for bar in bars:
            if not bar.is_closed:
                skipped_open += 1
                continue
            problems = bar_anomalies(bar)
            if problems:
                anomalies.append((bar, problems))
            else:
                good.append(bar)

        inserted = updated = 0
        with self._conn:  # одна транзакция: либо весь диапазон, либо ничего
            for bar in good:
                exists = self._conn.execute(
                    "SELECT 1 FROM ohlcv WHERE venue=? AND symbol=? AND timeframe=? AND ts_open=?",
                    (bar.venue, bar.symbol, bar.timeframe, bar.ts_open),
                ).fetchone()
                cur = self._conn.execute(
                    _UPSERT, {**bar.model_dump(exclude={"is_closed"}), "ingested_ts": now}
                )
                if cur.rowcount:
                    if exists:
                        updated += 1
                    else:
                        inserted += 1
            for bar, problems in anomalies:
                append_audit(
                    self._conn,
                    actor="data.loader",
                    event="DATA_ANOMALY",
                    severity="WARN",
                    ref_type="ohlcv",
                    ref_id=f"{bar.venue}|{bar.symbol}|{bar.timeframe}|{bar.ts_open}",
                    payload={"problems": problems, "bar": bar.model_dump()},
                    ts=now,
                )
        return LoadResult(
            fetched=len(bars),
            inserted=inserted,
            updated=updated,
            skipped_open=skipped_open,
            rejected=len(anomalies),
        )
