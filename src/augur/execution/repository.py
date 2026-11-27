"""Репозиторий `orders` / `fills` (ARCHITECTURE.md §3.2, §7.3).

Синхронный `sqlite3`, как остальные репозитории до появления `DbWriter` (отложен до
оркестратора). Методы не делают COMMIT: транзакцией управляет `OrderManager`.

- `insert_pending` идемпотентен по `UNIQUE(venue, client_order_id)`.
- `update` — optimistic locking по `version`; переход проверяется state machine.
- `insert_fill` идемпотентен по `UNIQUE(order_id, venue_fill_id)`.
"""

import sqlite3
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from augur.adapters.base import OrderState
from augur.execution.state_machine import ACTIVE_STATES, check_transition
from augur.ids import new_ulid

__all__ = ["FillRecord", "OrderRecord", "OrderRepository", "StaleOrderError"]

_COLS = (
    "id, client_order_id, venue_order_id, signal_id, risk_verdict_id, venue, symbol, side, type, "
    "qty, limit_price, stop_price, filled_qty, avg_fill_price, fee, fee_ccy, state, mode, "
    "reject_reason, attempt, created_ts, updated_ts, version"
)


def _dec(v: object) -> Decimal | None:
    return None if v is None else Decimal(str(v))


class StaleOrderError(RuntimeError):
    """Строка ордера изменилась с момента чтения (optimistic locking)."""


class OrderRecord(BaseModel):
    """Строка `orders`. Деньги — `Decimal`, время — Unix ms UTC."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    client_order_id: str
    venue_order_id: str | None
    signal_id: str
    risk_verdict_id: str
    venue: str
    symbol: str
    side: Literal["BUY", "SELL"]
    type: Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT"]
    qty: Decimal
    limit_price: Decimal | None
    stop_price: Decimal | None
    filled_qty: Decimal
    avg_fill_price: Decimal | None
    fee: Decimal
    fee_ccy: str | None
    state: OrderState
    mode: str
    reject_reason: str | None
    attempt: int
    created_ts: int
    updated_ts: int
    version: int

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "OrderRecord":
        names = [c.strip() for c in _COLS.split(",")]
        d = dict(zip(names, row, strict=True))
        for k in ("qty", "filled_qty", "fee"):
            d[k] = Decimal(str(d[k]))
        for k in ("limit_price", "stop_price", "avg_fill_price"):
            d[k] = _dec(d[k])
        d["state"] = OrderState(str(d["state"]))
        return cls.model_validate(d)


class FillRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    order_id: str
    venue_fill_id: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    ts: int


class OrderRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    # --- чтение -----------------------------------------------------------------------

    def get(self, order_id: str) -> OrderRecord | None:
        row = self._conn.execute(f"SELECT {_COLS} FROM orders WHERE id = ?", (order_id,)).fetchone()  # noqa: S608 — константа
        return None if row is None else OrderRecord.from_row(row)

    def get_by_coid(self, venue: str, client_order_id: str) -> OrderRecord | None:
        row = self._conn.execute(
            f"SELECT {_COLS} FROM orders WHERE venue = ? AND client_order_id = ?",  # noqa: S608
            (venue, client_order_id),
        ).fetchone()
        return None if row is None else OrderRecord.from_row(row)

    def active(self, *, mode: str | None = None, symbol: str | None = None) -> list[OrderRecord]:
        marks = ",".join("?" * len(ACTIVE_STATES))
        sql = f"SELECT {_COLS} FROM orders WHERE state IN ({marks})"  # noqa: S608 — плейсхолдеры
        params: list[object] = [s.value for s in sorted(ACTIVE_STATES)]
        if mode is not None:
            sql += " AND mode = ?"
            params.append(mode)
        if symbol is not None:
            sql += " AND symbol = ?"
            params.append(symbol)
        rows = self._conn.execute(sql + " ORDER BY created_ts, id", params).fetchall()
        return [OrderRecord.from_row(r) for r in rows]

    def recent(self, *, limit: int = 50, state: str | None = None) -> list[OrderRecord]:
        sql = f"SELECT {_COLS} FROM orders"  # noqa: S608 — константа
        params: list[object] = []
        if state is not None:
            sql += " WHERE state = ?"
            params.append(state)
        sql += " ORDER BY created_ts DESC, id DESC LIMIT ?"
        params.append(limit)
        return [OrderRecord.from_row(r) for r in self._conn.execute(sql, params).fetchall()]

    def fills(self, order_id: str) -> list[FillRecord]:
        rows = self._conn.execute(
            "SELECT id, order_id, venue_fill_id, qty, price, fee, ts FROM fills "
            "WHERE order_id = ? ORDER BY ts, id",
            (order_id,),
        ).fetchall()
        return [
            FillRecord(
                id=str(r[0]),
                order_id=str(r[1]),
                venue_fill_id=str(r[2]),
                qty=Decimal(str(r[3])),
                price=Decimal(str(r[4])),
                fee=Decimal(str(r[5])),
                ts=int(r[6]),
            )
            for r in rows
        ]

    def signal_action(self, signal_id: str) -> str | None:
        row = self._conn.execute("SELECT action FROM signals WHERE id = ?", (signal_id,)).fetchone()
        return None if row is None else str(row[0])

    # --- запись -----------------------------------------------------------------------

    def insert_pending(
        self,
        *,
        client_order_id: str,
        signal_id: str,
        risk_verdict_id: str,
        venue: str,
        symbol: str,
        side: Literal["BUY", "SELL"],
        qty: Decimal,
        mode: str,
        ts: int,
        order_type: Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT"] = "MARKET",
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
        attempt: int = 0,
    ) -> tuple[OrderRecord, bool]:
        """PENDING-ордер. Returns: (запись, создан ли новый). Дубль → (существующий, False)."""
        cur = self._conn.execute(
            "INSERT INTO orders(id, client_order_id, signal_id, risk_verdict_id, venue, symbol, "
            "side, type, qty, limit_price, stop_price, state, mode, attempt, created_ts, "
            "updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?) "
            "ON CONFLICT(venue, client_order_id) DO NOTHING",
            (
                new_ulid(ts),
                client_order_id,
                signal_id,
                risk_verdict_id,
                venue,
                symbol,
                side,
                order_type,
                str(qty),
                None if limit_price is None else str(limit_price),
                None if stop_price is None else str(stop_price),
                mode,
                attempt,
                ts,
                ts,
            ),
        )
        rec = self.get_by_coid(venue, client_order_id)
        assert rec is not None  # noqa: S101 — только что вставлена или уже была
        return rec, cur.rowcount == 1

    def update(
        self,
        order: OrderRecord,
        *,
        state: OrderState,
        ts: int,
        venue_order_id: str | None = None,
        filled_qty: Decimal | None = None,
        avg_fill_price: Decimal | None = None,
        fee: Decimal | None = None,
        fee_ccy: str | None = None,
        reject_reason: str | None = None,
    ) -> OrderRecord:
        """Переход состояния с `version + 1`.

        Raises:
            IllegalTransitionError: переход не из таблицы ARCH §7.3.
            StaleOrderError: строка изменена параллельно.
        """
        if state != order.state or state == OrderState.PARTIAL:
            check_transition(order.state, state)
        cur = self._conn.execute(
            "UPDATE orders SET state = ?, venue_order_id = COALESCE(?, venue_order_id), "
            "filled_qty = COALESCE(?, filled_qty), avg_fill_price = COALESCE(?, avg_fill_price), "
            "fee = COALESCE(?, fee), fee_ccy = COALESCE(?, fee_ccy), "
            "reject_reason = COALESCE(?, reject_reason), updated_ts = ?, version = version + 1 "
            "WHERE id = ? AND version = ?",
            (
                state.value,
                venue_order_id,
                None if filled_qty is None else str(filled_qty),
                None if avg_fill_price is None else str(avg_fill_price),
                None if fee is None else str(fee),
                fee_ccy,
                reject_reason,
                ts,
                order.id,
                order.version,
            ),
        )
        if cur.rowcount != 1:
            raise StaleOrderError(f"order {order.id} changed (version {order.version})")
        rec = self.get(order.id)
        assert rec is not None  # noqa: S101
        return rec

    def insert_fill(
        self,
        order_id: str,
        *,
        venue_fill_id: str,
        qty: Decimal,
        price: Decimal,
        fee: Decimal,
        ts: int,
    ) -> FillRecord | None:
        """Новый fill или None — уже был (дедуп по `(order_id, venue_fill_id)`)."""
        fill_id = new_ulid(ts)
        cur = self._conn.execute(
            "INSERT INTO fills(id, order_id, venue_fill_id, qty, price, fee, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(order_id, venue_fill_id) DO NOTHING",
            (fill_id, order_id, venue_fill_id, str(qty), str(price), str(fee), ts),
        )
        if cur.rowcount != 1:
            return None
        return FillRecord(
            id=fill_id,
            order_id=order_id,
            venue_fill_id=venue_fill_id,
            qty=qty,
            price=price,
            fee=fee,
            ts=ts,
        )
