"""Ручное подтверждение первого ордера MICRO_LIVE (ARCHITECTURE.md §7.1, FUSION SPEC [Д7]).

Первый ордер после входа в MICRO_LIVE (`mode_state.micro_first_order_confirmed = 0`) не
отправляется: `OrderManager` оставляет его в PENDING и в той же транзакции пишет
`ORDER_APPROVAL_REQUIRED`. Подтверждение — `augur orders approve <id> --confirm`:
1) одноразовый 6-значный код (TTL `mode.code_ttl_s`, только в памяти процесса);
2) проверки: ордер PENDING и ждёт подтверждения, режим MICRO_LIVE, CB ARMED, TTL сигнала
   не истёк (ожидание не дольше TTL — FUSION SPEC [Д7]);
3) одной транзакцией `micro_first_order_confirmed = 1` и `ORDER_APPROVED`;
4) `OrderManager.submit` — отправка на площадку.
Если TTL сигнала истёк, ордер становится REJECTED (`OrderManager.expire_unapproved`, его же
вызывает Reconciler). Остальные ордера режима после подтверждения уходят автоматически.
Системные выходы (SL/TP/flatten) подтверждения не ждут: они снижают риск.
"""

import secrets
import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from augur.adapters.base import OrderState
from augur.config.settings import TradingMode
from augur.db.audit import append_audit

if TYPE_CHECKING:
    from augur.execution.modes import ModeStore
    from augur.execution.order_manager import OrderManager
    from augur.execution.repository import OrderRecord

__all__ = [
    "APPROVAL_REQUIRED",
    "APPROVED",
    "ApprovalError",
    "ApprovalTicket",
    "FirstOrderApproval",
    "InvalidApprovalCodeError",
    "awaiting_approval",
    "first_order_confirmed",
]

APPROVAL_REQUIRED = "ORDER_APPROVAL_REQUIRED"
APPROVED = "ORDER_APPROVED"


def first_order_confirmed(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT micro_first_order_confirmed FROM mode_state WHERE id = 1").fetchone()
    return row is not None and int(row[0]) == 1


def awaiting_approval(conn: sqlite3.Connection, order_id: str) -> bool:
    """Ордер ждёт ручного подтверждения: есть `ORDER_APPROVAL_REQUIRED` и нет `ORDER_APPROVED`."""
    rows = {
        str(r[0])
        for r in conn.execute(
            "SELECT event FROM audit_log WHERE ref_type = 'order' AND ref_id = ? "
            "AND event IN (?, ?)",
            (order_id, APPROVAL_REQUIRED, APPROVED),
        )
    }
    return APPROVAL_REQUIRED in rows and APPROVED not in rows


class ApprovalError(PermissionError):
    """Подтверждение невозможно: ордер, режим, CB или TTL."""


class InvalidApprovalCodeError(ApprovalError):
    """Неверный, просроченный или уже использованный код."""


class ApprovalTicket(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str
    code: str
    expires_ts: int


class FirstOrderApproval:
    """Args:
    conn: соединение.
    orders: OrderManager режима MICRO_LIVE (с реальным адаптером).
    modes: хранилище режима.
    clock: «сейчас», Unix ms UTC.
    code_ttl_s: TTL одноразового кода.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        orders: "OrderManager",
        modes: "ModeStore",
        clock: Callable[[], int],
        code_ttl_s: int = 120,
    ) -> None:
        self._conn = conn
        self._om = orders
        self._modes = modes
        self._clock = clock
        self._ttl_ms = code_ttl_s * 1000
        self._tickets: dict[str, ApprovalTicket] = {}

    def pending(self) -> list["OrderRecord"]:
        """Ордера режима, ждущие подтверждения."""
        return [
            o
            for o in self._om.orders.active(mode=self._om.mode.value)
            if o.state == OrderState.PENDING and awaiting_approval(self._conn, o.id)
        ]

    def _check(self, order_id: str) -> "OrderRecord":
        order = self._om.orders.get(order_id)
        if order is None:
            raise ApprovalError(f"order not found: {order_id}")
        if order.mode != TradingMode.MICRO_LIVE.value or order.venue != self._om.venue:
            raise ApprovalError(f"order {order_id} is not a MICRO_LIVE order on {self._om.venue}")
        if order.state != OrderState.PENDING or not awaiting_approval(self._conn, order_id):
            raise ApprovalError(f"order {order_id} does not await approval ({order.state})")
        mode = self._modes.current()
        if mode != TradingMode.MICRO_LIVE:
            raise ApprovalError(f"mode is {mode}, approval requires MICRO_LIVE")
        row = self._conn.execute("SELECT state FROM cb_state WHERE id = 1").fetchone()
        if row is not None and row[0] != "ARMED":
            raise ApprovalError(f"circuit breaker {row[0]}: new orders blocked")
        return order

    def issue_code(self, order_id: str) -> ApprovalTicket:
        self._check(order_id)
        ticket = ApprovalTicket(
            order_id=order_id,
            code=f"{secrets.randbelow(1_000_000):06d}",
            expires_ts=self._clock() + self._ttl_ms,
        )
        self._tickets[order_id] = ticket
        return ticket

    async def approve(self, order_id: str, code: str, *, actor: str) -> "OrderRecord":
        """Код → проверки → `ORDER_APPROVED` + флаг (COMMIT) → отправка.

        Raises:
            InvalidApprovalCodeError: код неверен или просрочен (тикет сгорает).
            ApprovalError: ордер не ждёт подтверждения, режим/CB не позволяют, TTL истёк.
        """
        ticket = self._tickets.pop(order_id, None)
        now = self._clock()
        if (
            ticket is None
            or now > ticket.expires_ts
            or not secrets.compare_digest(ticket.code, code)
        ):
            raise InvalidApprovalCodeError("invalid or expired confirmation code")
        order = self._check(order_id)
        if order.id in await self._om.expire_unapproved():
            raise ApprovalError(f"signal TTL expired: order {order_id} rejected")
        try:
            self._conn.execute("UPDATE mode_state SET micro_first_order_confirmed = 1 WHERE id = 1")
            append_audit(
                self._conn,
                actor=actor,
                event=APPROVED,
                severity="WARN",
                ref_type="order",
                ref_id=order.id,
                payload={
                    "mode": order.mode,
                    "client_order_id": order.client_order_id,
                    "symbol": order.symbol,
                    "side": order.side,
                    "qty": str(order.qty),
                },
                ts=now,
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return await self._om.submit(order)
