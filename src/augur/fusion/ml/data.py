"""Чтение закрытых баров из `ohlcv` для обучения и инференса (FUSION SPEC §6.1: только закрытые)."""

import sqlite3

import numpy as np

from augur.data.models import timeframe_ms
from augur.fusion.ml.features import BarArrays

__all__ = ["load_bar_arrays"]


def load_bar_arrays(
    conn: sqlite3.Connection,
    *,
    venue: str,
    symbol: str,
    timeframe: str,
    until_close: int | None = None,
    since_open: int | None = None,
    limit: int | None = None,
) -> BarArrays | None:
    """Бары с `close_time <= until_close` (последние `limit`), по возрастанию `ts_open`."""
    tf = timeframe_ms(timeframe)
    sql = (
        "SELECT ts_open, open, high, low, close, volume FROM ohlcv "
        "WHERE venue=? AND symbol=? AND timeframe=?"
    )
    args: list[object] = [venue, symbol, timeframe]
    if until_close is not None:
        sql += " AND ts_open <= ?"
        args.append(until_close - tf)
    if since_open is not None:
        sql += " AND ts_open >= ?"
        args.append(since_open)
    sql += " ORDER BY ts_open DESC"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    rows = conn.execute(sql, args).fetchall()[::-1]
    if not rows:
        return None
    arr = np.array(rows, dtype=np.float64)
    return BarArrays(
        timeframe=timeframe,
        ts_open=np.array([r[0] for r in rows], dtype=np.int64),
        open=arr[:, 1],
        high=arr[:, 2],
        low=arr[:, 3],
        close=arr[:, 4],
        volume=arr[:, 5],
    )
