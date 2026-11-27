"""Позиции и снимки портфеля (ARCHITECTURE.md §3.2, §7.3).

- `PositionManager.apply_fill` — позиция обновляется по КАЖДОМУ новому fill (частичные
  исполнения тоже). Средняя цена: открытие/добор — взвешенная; частичное закрытие — не меняется;
  переворот — остаток по цене fill. `realized_pnl` копит PnL закрытой части минус комиссии.
- `PortfolioTracker.snapshot` — строка `portfolio_snapshots`: `day_pnl` от equity на начало
  UTC-дня, `peak_equity`, `drawdown_pct` (R10 Risk Gate и CB-триггеры читают её).

Без COMMIT — транзакцией управляет вызывающий.
"""

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from augur.adapters.base import AccountState

__all__ = ["FillEffect", "PortfolioSnapshot", "PortfolioTracker", "PositionManager", "PositionRow"]

_Q = Decimal("0.00000001")


class PositionRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    venue: str
    symbol: str
    mode: str
    qty: Decimal
    avg_price: Decimal
    realized_pnl: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    opened_ts: int | None
    updated_ts: int


class FillEffect(BaseModel):
    """Результат применения fill к позиции."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    realized_pnl: Decimal  # PnL закрытой части − комиссия этого fill
    closed_qty: Decimal  # сколько позиции закрыто этим fill (≥ 0)
    trade_closed: bool  # позиция закрыта в ноль или перевёрнута — «закрытая сделка» для CB
    position: PositionRow


def _sign(x: Decimal) -> int:
    return (x > 0) - (x < 0)


class PositionManager:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, venue: str, symbol: str, mode: str) -> PositionRow | None:
        row = self._conn.execute(
            "SELECT venue, symbol, mode, qty, avg_price, realized_pnl, stop_loss, take_profit, "
            "opened_ts, updated_ts FROM positions WHERE venue = ? AND symbol = ? AND mode = ?",
            (venue, symbol, mode),
        ).fetchone()
        return None if row is None else self._row(row)

    def all(self, mode: str, *, venue: str | None = None) -> list[PositionRow]:
        sql = (
            "SELECT venue, symbol, mode, qty, avg_price, realized_pnl, stop_loss, take_profit, "
            "opened_ts, updated_ts FROM positions WHERE mode = ?"
        )
        params: list[object] = [mode]
        if venue is not None:
            sql += " AND venue = ?"
            params.append(venue)
        return [self._row(r) for r in self._conn.execute(sql + " ORDER BY symbol", params)]

    def open(self, mode: str, *, venue: str | None = None) -> list[PositionRow]:
        return [p for p in self.all(mode, venue=venue) if p.qty != 0]

    @staticmethod
    def _row(r: tuple[object, ...]) -> PositionRow:
        return PositionRow(
            venue=str(r[0]),
            symbol=str(r[1]),
            mode=str(r[2]),
            qty=Decimal(str(r[3])),
            avg_price=Decimal(str(r[4])),
            realized_pnl=Decimal(str(r[5])),
            stop_loss=None if r[6] is None else Decimal(str(r[6])),
            take_profit=None if r[7] is None else Decimal(str(r[7])),
            opened_ts=None if r[8] is None else int(str(r[8])),
            updated_ts=int(str(r[9])),
        )

    def apply_fill(
        self,
        *,
        venue: str,
        symbol: str,
        mode: str,
        side: Literal["BUY", "SELL"],
        qty: Decimal,
        price: Decimal,
        fee: Decimal,
        ts: int,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> FillEffect:
        """Применить fill. `stop_loss`/`take_profit` ставятся при открытии/доборе/перевороте."""
        cur = self.get(venue, symbol, mode)
        q = cur.qty if cur else Decimal(0)
        avg = cur.avg_price if cur else Decimal(0)
        realized = cur.realized_pnl if cur else Decimal(0)
        sl = cur.stop_loss if cur else None
        tp = cur.take_profit if cur else None
        opened = cur.opened_ts if cur else None
        signed = qty if side == "BUY" else -qty
        new_q = q + signed
        closed = Decimal(0)
        pnl = Decimal(0)
        if q == 0 or _sign(q) == _sign(signed):
            avg = ((q * avg + signed * price) / new_q).quantize(_Q)
            opened = opened if q != 0 else ts
            sl = stop_loss if stop_loss is not None else sl
            tp = take_profit if take_profit is not None else tp
        else:
            closed = min(abs(signed), abs(q))
            pnl = closed * (price - avg) * _sign(q)
            if new_q == 0:
                avg, opened, sl, tp = Decimal(0), None, None, None
            elif _sign(new_q) != _sign(q):  # переворот
                avg, opened, sl, tp = price.quantize(_Q), ts, stop_loss, take_profit
        delta = (pnl - fee).quantize(_Q)
        realized = (realized + delta).quantize(_Q)
        self._conn.execute(
            "INSERT INTO positions(venue, symbol, mode, qty, avg_price, realized_pnl, stop_loss, "
            "take_profit, opened_ts, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(venue, symbol, mode) DO UPDATE SET qty = excluded.qty, "
            "avg_price = excluded.avg_price, realized_pnl = excluded.realized_pnl, "
            "stop_loss = excluded.stop_loss, take_profit = excluded.take_profit, "
            "opened_ts = excluded.opened_ts, updated_ts = excluded.updated_ts",
            (
                venue,
                symbol,
                mode,
                str(new_q),
                str(avg),
                str(realized),
                None if sl is None else str(sl),
                None if tp is None else str(tp),
                opened,
                ts,
            ),
        )
        pos = self.get(venue, symbol, mode)
        assert pos is not None  # noqa: S101
        trade_closed = closed > 0 and (new_q == 0 or _sign(new_q) != _sign(q))
        return FillEffect(
            realized_pnl=delta, closed_qty=closed, trade_closed=trade_closed, position=pos
        )


class PortfolioSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: int
    mode: str
    equity: Decimal
    cash: Decimal
    margin_used: Decimal
    day_pnl: Decimal
    peak_equity: Decimal
    drawdown_pct: Decimal  # доля: 0.1 = 10 %


def _day_start_ms(ts: int) -> int:
    d = datetime.fromtimestamp(ts / 1000, tz=UTC)
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


class PortfolioTracker:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def latest(self, mode: str) -> PortfolioSnapshot | None:
        row = self._conn.execute(
            "SELECT ts, mode, equity, cash, margin_used, day_pnl, peak_equity, drawdown_pct "
            "FROM portfolio_snapshots WHERE mode = ? ORDER BY ts DESC LIMIT 1",
            (mode,),
        ).fetchone()
        if row is None:
            return None
        return PortfolioSnapshot(
            ts=int(row[0]),
            mode=str(row[1]),
            equity=Decimal(str(row[2])),
            cash=Decimal(str(row[3])),
            margin_used=Decimal(str(row[4])),
            day_pnl=Decimal(str(row[5])),
            peak_equity=Decimal(str(row[6])),
            drawdown_pct=Decimal(str(row[7])),
        )

    def _equity_start_of_day(self, mode: str, ts: int, equity: Decimal) -> Decimal:
        day0 = _day_start_ms(ts)
        before = self._conn.execute(
            "SELECT equity FROM portfolio_snapshots WHERE mode = ? AND ts < ? "
            "ORDER BY ts DESC LIMIT 1",
            (mode, day0),
        ).fetchone()
        if before is not None:
            return Decimal(str(before[0]))
        first = self._conn.execute(
            "SELECT equity FROM portfolio_snapshots WHERE mode = ? AND ts >= ? AND ts < ? "
            "ORDER BY ts LIMIT 1",
            (mode, day0, ts),
        ).fetchone()
        return Decimal(str(first[0])) if first is not None else equity

    def snapshot(
        self, mode: str, account: AccountState, positions: list[PositionRow], *, ts: int
    ) -> PortfolioSnapshot:
        """Записать снимок. День — UTC [ДОПУЩЕНИЕ: календарь площадки — этап 0.7.0+]."""
        prev = self.latest(mode)
        sod = self._equity_start_of_day(mode, ts, account.equity)
        peak = max(account.equity, prev.peak_equity if prev else account.equity)
        dd = ((peak - account.equity) / peak).quantize(_Q) if peak > 0 else Decimal(0)
        snap = PortfolioSnapshot(
            ts=ts,
            mode=mode,
            equity=account.equity,
            cash=account.cash,
            margin_used=account.margin_used,
            day_pnl=(account.equity - sod).quantize(_Q),
            peak_equity=peak,
            drawdown_pct=dd,
        )
        positions_json = json.dumps(
            [
                {"symbol": p.symbol, "venue": p.venue, "qty": str(p.qty), "avg": str(p.avg_price)}
                for p in positions
                if p.qty != 0
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        self._conn.execute(
            "INSERT INTO portfolio_snapshots(ts, mode, equity, cash, margin_used, day_pnl, "
            "peak_equity, drawdown_pct, positions_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(mode, ts) DO UPDATE SET equity = excluded.equity, cash = excluded.cash, "
            "margin_used = excluded.margin_used, day_pnl = excluded.day_pnl, "
            "peak_equity = excluded.peak_equity, drawdown_pct = excluded.drawdown_pct, "
            "positions_json = excluded.positions_json",
            (
                ts,
                mode,
                str(snap.equity),
                str(snap.cash),
                str(snap.margin_used),
                str(snap.day_pnl),
                str(snap.peak_equity),
                str(snap.drawdown_pct),
                positions_json,
            ),
        )
        return snap
