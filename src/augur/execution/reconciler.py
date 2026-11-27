"""Reconciler (ARCHITECTURE.md §7.3): сверка ордеров и позиций БД с площадкой.

Каждые `execution.reconcile_interval_s` (30 с) и при реконнекте — до подписок:
1) активные ордера (PENDING / SUBMITTED / PARTIAL / UNKNOWN) → `get_order(coid)`:
   найден — переход к фактическому статусу; площадка ордера не знает — PENDING/UNKNOWN →
   REJECTED («не дошёл до площадки»); статус не установить — ордер остаётся как есть;
2) позиции: |БД − площадка| > `qty_step` → `RECONCILE_MISMATCH` (CRITICAL) и срабатывание
   CB `reconcile_mismatch` — новые входы блокируются до ручного разбора и `cb reset`.

[ДОПУЩЕНИЕ] ARCH §7.4 называет триггер «любое расхождение позиций в live»; в PAPER БД и
PaperAdapter должны совпадать точно, расхождение — дефект, поэтому CB срабатывает и здесь.

MICRO_LIVE, spot (0.7.0, решение пользователя): торговля идёт на выделенном субсчёте, где на
входе в режим нет базовых активов. Сверяются символы с позициями в БД и `symbols`
(`settings.symbols`); прочие балансы субсчёта (пыль, чужие монеты) не сверяются. Допуск —
max(`qty_step`, `min_qty`): комиссия в базовой валюте уменьшает остаток на доли шага.
Ордера, ждущие ручного подтверждения (`ORDER_APPROVAL_REQUIRED`), на площадку не уходили:
Reconciler их не трогает, а просроченные по TTL сигнала отклоняет (`expire_unapproved`).
"""

import sqlite3
from collections.abc import Callable, Iterable
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from augur.adapters.base import AdapterError, OrderState
from augur.db.audit import append_audit
from augur.execution.approval import awaiting_approval
from augur.execution.circuit_breaker import CircuitBreaker
from augur.execution.order_manager import OrderManager
from augur.risk.post_trade import CBTrigger

__all__ = ["PositionMismatch", "ReconcileReport", "Reconciler"]


class PositionMismatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    db_qty: Decimal
    venue_qty: Decimal
    tolerance: Decimal


class ReconcileReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    checked_orders: int
    updated_orders: list[str]  # id ордеров, чьё состояние изменилось
    unresolved_orders: list[str]  # статус не удалось установить
    mismatches: list[PositionMismatch]
    ts: int

    @property
    def ok(self) -> bool:
        return not self.mismatches and not self.unresolved_orders


class Reconciler:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        orders: OrderManager,
        breaker: CircuitBreaker | None,
        clock: Callable[[], int],
        symbols: Iterable[str] | None = None,
    ) -> None:
        """`symbols` — сверяемые символы площадки (MICRO_LIVE spot); None — все позиции площадки."""
        self._conn = conn
        self._om = orders
        self._cb = breaker
        self._clock = clock
        self._symbols = None if symbols is None else frozenset(symbols)

    async def run(self) -> ReconcileReport:
        adapter = self._om.adapter
        updated: list[str] = list(await self._om.expire_unapproved())
        unresolved: list[str] = []
        active = self._om.orders.active(mode=self._om.mode.value)
        for order in active:
            if order.state == OrderState.PENDING and awaiting_approval(self._conn, order.id):
                continue  # не отправлялся: ждёт `augur orders approve`
            try:
                ack = await adapter.get_order(order.client_order_id, order.symbol)
            except AdapterError:
                unresolved.append(order.id)
                continue
            if ack is None:
                if order.state in (OrderState.PENDING, OrderState.UNKNOWN):
                    rec = await self._om.apply_ack(
                        order,
                        self._om.rejected_ack(order, "reconcile: order not found at venue"),
                        actor="reconciler",
                    )
                    updated.append(rec.id)
                else:
                    unresolved.append(order.id)  # SUBMITTED/PARTIAL пропал на площадке
                continue
            rec = await self._om.apply_ack(order, ack, actor="reconciler", note="reconcile")
            if rec.state != order.state or rec.filled_qty != order.filled_qty:
                updated.append(rec.id)

        mismatches = await self._positions()
        now = self._clock()
        if mismatches:
            try:
                for m in mismatches:
                    append_audit(
                        self._conn,
                        actor="reconciler",
                        event="RECONCILE_MISMATCH",
                        severity="CRITICAL",
                        ref_type="order",
                        ref_id=m.symbol,
                        payload={
                            "mode": self._om.mode.value,
                            "symbol": m.symbol,
                            "db_qty": str(m.db_qty),
                            "venue_qty": str(m.venue_qty),
                            "tolerance": str(m.tolerance),
                        },
                        ts=now,
                    )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            if self._cb is not None:
                await self._cb.trip(
                    CBTrigger.RECONCILE_MISMATCH,
                    ", ".join(f"{m.symbol} db={m.db_qty} venue={m.venue_qty}" for m in mismatches),
                    actor="reconciler",
                )
        return ReconcileReport(
            checked_orders=len(active),
            updated_orders=updated,
            unresolved_orders=unresolved,
            mismatches=mismatches,
            ts=now,
        )

    async def check_positions(self) -> list[PositionMismatch]:
        """Только сравнение позиций БД и площадки: без аудита и без CB (preflight `mode set`)."""
        return await self._positions()

    async def _positions(self) -> list[PositionMismatch]:
        adapter = self._om.adapter
        db = {
            p.symbol: p.qty
            for p in self._om.positions.all(self._om.mode.value, venue=self._om.venue)
        }
        venue = {p.symbol: p.qty for p in await adapter.get_positions()}
        scope = set(venue) if self._symbols is None else set(venue) & self._symbols
        live = self._om.venue != "paper"
        out = []
        for symbol in sorted(set(db) | scope):
            d, v = db.get(symbol, Decimal(0)), venue.get(symbol, Decimal(0))
            if d == v:
                continue
            spec = await adapter.get_instrument(symbol)
            tol = max(spec.qty_step, spec.min_qty) if live else spec.qty_step
            if abs(d - v) > tol:
                out.append(PositionMismatch(symbol=symbol, db_qty=d, venue_qty=v, tolerance=tol))
        return out
