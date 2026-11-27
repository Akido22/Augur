"""OrderManager (ARCHITECTURE.md §7.1, §7.3, VERSIONING.md §6.2) — режимы PAPER и MICRO_LIVE.

Путь ордера: `RiskVerdict(approved)` → проверка закоммиченной записи `decision_audit`
(полнота версий, §6.2) → `orders` PENDING (идемпотентно по детерминированному
`client_order_id`) + `decision_audit.order_id` → COMMIT → `place_order` → переходы state
machine по ответу → fills (дедуп) → позиции → `decision_audit.execution_result` (один раз).

Сеть (ARCH §4.2): `place_order` не повторяется вслепую. Таймаут/сбой → `get_order(coid)`;
найден — берём его статус; площадка ответила «нет такого» — одна повторная отправка с ТЕМ ЖЕ
`client_order_id`; статус не установить — `UNKNOWN` (R5 блокирует символ до реконсиляции).

Системные выходы — защитные SL/TP и закрытие позиций kill switch / CB — снижают риск и идут
мимо сигнального контура и Risk Gate (ARCH §7.4: «через адаптер, минуя сигнальный контур, но с
записью в аудит»): для них пишется собственный `signals(kind='final', action='CLOSE')` и
`decision_audit` с `strategy_version='system:<leg>'`, ордер — только `reduce_only`.
Защитные выходы не блокируются ни CB, ни kill switch (skill auto-trade-risk-gate).

Режимы: PAPER — только с адаптером `paper`; MICRO_LIVE (0.7.0) — только с реальным адаптером
(не `paper`). Первый ордер после входа в MICRO_LIVE остаётся PENDING и ждёт ручного
подтверждения (`ORDER_APPROVAL_REQUIRED`, `augur.execution.approval`). FULL_AUTO — 0.11.0,
менеджер его отклоняет.
"""

import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, Literal

from augur.adapters.base import (
    AdapterError,
    MarketAdapter,
    OrderAck,
    OrderRequest,
    OrderState,
    RejectedError,
    VenueFill,
    VenuePosition,
)
from augur.adapters.paper import PaperAdapter
from augur.config.settings import ExecutionSettings, TradingMode
from augur.db.audit import append_audit
from augur.execution.approval import APPROVAL_REQUIRED, awaiting_approval, first_order_confirmed
from augur.execution.idempotency import make_client_order_id
from augur.execution.positions import PortfolioSnapshot, PortfolioTracker, PositionManager
from augur.execution.repository import OrderRecord, OrderRepository
from augur.execution.state_machine import TERMINAL_STATES, IllegalTransitionError, path_to
from augur.ids import new_ulid
from augur.risk.audit import NO_ARTIFACT, DecisionAuditRecord, assert_auditable
from augur.risk.gate import order_side
from augur.risk.models import RiskVerdict
from augur.risk.post_trade import FillEvent
from augur.signals.ready import SignalReady

__all__ = [
    "ACTOR",
    "ExecutionRefusedError",
    "OrderManager",
    "restore_paper_broker",
]

log = logging.getLogger(__name__)

ACTOR = "execution"
_Q = Decimal("0.00000001")
_SYSTEM_TTL_MS = 60_000
_OPENING_ACTIONS = ("BUY", "SELL")

FillHook = Callable[[FillEvent], Awaitable[object]]


class ExecutionRefusedError(RuntimeError):
    """Ордер не может быть создан: режим, состояние CB/kill switch или несогласованный вердикт."""


def _j(v: object) -> str | None:
    return None if v is None else str(v)


class OrderManager:
    """Args:
    conn: соединение с миграциями 0005+.
    adapter: `ResilientAdapter` над `PaperAdapter` (PAPER) или реальным адаптером (MICRO_LIVE).
    mode: режим исполнения: PAPER или MICRO_LIVE.
    app_version / config_hash: для системных записей `decision_audit` и сигналов.
    settings: `settings.execution`.
    clock: «сейчас», Unix ms UTC.
    on_fill: хук после каждого нового fill (PostTradeMonitor.on_fill).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        adapter: MarketAdapter,
        *,
        mode: TradingMode,
        app_version: str,
        config_hash: str,
        settings: ExecutionSettings | None = None,
        clock: Callable[[], int],
        on_fill: FillHook | None = None,
    ) -> None:
        if mode == TradingMode.PAPER:
            if adapter.venue != "paper":
                raise ExecutionRefusedError(
                    f"PAPER requires the paper venue, got {adapter.venue!r}"
                )
        elif mode == TradingMode.MICRO_LIVE:
            if adapter.venue == "paper":
                raise ExecutionRefusedError("MICRO_LIVE requires a real venue, got 'paper'")
        else:
            raise ExecutionRefusedError(
                f"execution supports PAPER and MICRO_LIVE, got {mode} (FULL_AUTO: 0.11.0)"
            )
        self._conn = conn
        self._adapter = adapter
        self._mode = mode
        self._app_version = app_version
        self._config_hash = config_hash
        self._settings = settings or ExecutionSettings()
        self._clock = clock
        self.on_fill = on_fill
        self.orders = OrderRepository(conn)
        self.positions = PositionManager(conn)
        self.tracker = PortfolioTracker(conn)

    @property
    def mode(self) -> TradingMode:
        return self._mode

    @property
    def venue(self) -> str:
        return self._adapter.venue

    @property
    def adapter(self) -> MarketAdapter:
        return self._adapter

    # --- состояние CB ---------------------------------------------------------------

    def _cb_state(self) -> str:
        row = self._conn.execute("SELECT state FROM cb_state WHERE id = 1").fetchone()
        return "ARMED" if row is None else str(row[0])

    def _audit(
        self,
        event: str,
        order: OrderRecord | None,
        payload: dict[str, Any],
        *,
        severity: str = "INFO",
        actor: str = ACTOR,
        ts: int | None = None,
    ) -> None:
        append_audit(
            self._conn,
            actor=actor,
            event=event,
            severity=severity,
            ref_type="order" if order is not None else None,
            ref_id=order.id if order is not None else None,
            payload={"mode": self._mode.value, **payload},
            ts=ts if ts is not None else self._clock(),
        )

    # --- decision_audit -------------------------------------------------------------

    def _decision(
        self, signal_id: str, decision_id: str | None
    ) -> tuple[DecisionAuditRecord, str | None]:
        """Закоммиченная запись решения и `order_id`, если ордер по ней уже создан."""
        sql = (
            "SELECT id, signal_id, fused_signal_id, app_version, strategy_version, "
            "ml_model_version, llm_prompt_version, ml_prediction_id, llm_status, risk_verdict, "
            "risk_reason, config_hash, mode, created_at, order_id FROM decision_audit "
        )
        if decision_id is not None:
            row = self._conn.execute(
                sql + "WHERE id = ? AND signal_id = ?", (decision_id, signal_id)
            ).fetchone()
        else:
            row = self._conn.execute(
                sql + "WHERE signal_id = ? ORDER BY created_at DESC, id DESC LIMIT 1", (signal_id,)
            ).fetchone()
        if row is None:
            raise ExecutionRefusedError(f"no committed decision_audit for signal {signal_id}")
        names = (
            "id signal_id fused_signal_id app_version strategy_version ml_model_version "
            "llm_prompt_version ml_prediction_id llm_status risk_verdict risk_reason config_hash "
            "mode created_at"
        ).split()
        rec = DecisionAuditRecord.model_validate(dict(zip(names, row[:-1], strict=True)))
        assert_auditable(rec)  # VERSIONING.md §6.2: без полного набора версий ордера нет
        return rec, None if row[-1] is None else str(row[-1])

    def _link_decision(self, decision_id: str, order_id: str) -> None:
        cur = self._conn.execute(
            "UPDATE decision_audit SET order_id = ? WHERE id = ? AND order_id IS NULL",
            (order_id, decision_id),
        )
        if cur.rowcount != 1:
            raise ExecutionRefusedError(f"decision {decision_id} already linked")

    def _write_execution_result(self, order: OrderRecord) -> None:
        result = {
            "client_order_id": order.client_order_id,
            "venue_order_id": order.venue_order_id,
            "state": order.state.value,
            "filled_qty": str(order.filled_qty),
            "avg_fill_price": _j(order.avg_fill_price),
            "fee": str(order.fee),
            "reject_reason": order.reject_reason,
            "ts": order.updated_ts,
        }
        self._conn.execute(
            "UPDATE decision_audit SET execution_result = ? "
            "WHERE order_id = ? AND execution_result IS NULL",
            (json.dumps(result, sort_keys=True, separators=(",", ":")), order.id),
        )

    # --- вход по вердикту -----------------------------------------------------------

    async def handle_verdict(
        self, v: RiskVerdict, sig: SignalReady, *, decision_id: str | None = None
    ) -> OrderRecord | None:
        """approved → PENDING (идемпотентно) → submit. Неодобренный вердикт → None.

        Raises:
            ExecutionRefusedError: режим, CB/kill switch, нет записи `decision_audit`.
            UnauditableOrderError: запись аудита без полного набора версий (§6.2).
        """
        if not v.approved or v.sized_qty is None or v.sized_qty <= 0:
            return None
        if v.signal_id != sig.signal_id:
            raise ExecutionRefusedError("verdict does not belong to signal")
        if v.mode != self._mode:
            raise ExecutionRefusedError(f"verdict mode {v.mode} != execution mode {self._mode}")
        decision, linked = self._decision(sig.signal_id, decision_id)
        if linked is not None:  # повторная обработка того же решения (рестарт, дубль события)
            existing = self.orders.get(linked)
            assert existing is not None  # noqa: S101 — FK decision_audit.order_id
            self._suppress_duplicate(existing, sig.signal_id)
            return existing
        opening = sig.action.value in _OPENING_ACTIONS
        cb = self._cb_state()
        if cb == "KILLED" or (cb == "TRIPPED" and opening):
            raise ExecutionRefusedError(f"circuit breaker {cb}: new orders blocked")
        pos = self.positions.get(self.venue, sig.symbol, self._mode.value)
        side = order_side(sig, pos.qty if pos else Decimal(0))
        if side is None:
            return None
        coid = make_client_order_id(sig.signal_id, "entry", 0)
        needs_approval = self._requires_approval()
        order = self._create_pending(
            coid=coid,
            signal_id=sig.signal_id,
            risk_verdict_id=v.verdict_id,
            symbol=sig.symbol,
            side="BUY" if side == "BUY" else "SELL",
            qty=v.sized_qty,
            decision_id=decision.id,
            approval_expires_ts=int(sig.expires_at.timestamp() * 1000) if needs_approval else None,
        )
        if order is None:
            dup = self.orders.get_by_coid(self.venue, coid)
            assert dup is not None  # noqa: S101
            return dup
        if needs_approval:
            log.warning("MICRO_LIVE first order %s awaits manual approval", order.id)
            return order  # PENDING до `augur orders approve` (ARCH §7.1)
        return await self.submit(order)

    def _requires_approval(self) -> bool:
        """MICRO_LIVE: первый ордер после входа в режим ждёт ручного подтверждения."""
        return self._mode == TradingMode.MICRO_LIVE and not first_order_confirmed(self._conn)

    def _suppress_duplicate(self, order: OrderRecord, signal_id: str) -> None:
        """`ORDER_DUPLICATE_SUPPRESSED` (COMMIT). На площадку ничего не уходит."""
        try:
            self._audit(
                "ORDER_DUPLICATE_SUPPRESSED",
                order,
                {
                    "client_order_id": order.client_order_id,
                    "signal_id": signal_id,
                    "state": order.state.value,
                },
                severity="WARN",
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def _create_pending(
        self,
        *,
        coid: str,
        signal_id: str,
        risk_verdict_id: str,
        symbol: str,
        side: Literal["BUY", "SELL"],
        qty: Decimal,
        decision_id: str,
        approval_expires_ts: int | None = None,
    ) -> OrderRecord | None:
        """Новый PENDING + ссылка из decision_audit, одна транзакция. Дубль → None.

        `approval_expires_ts` задан — в той же транзакции `ORDER_APPROVAL_REQUIRED` (MICRO_LIVE).
        """
        now = self._clock()
        try:
            order, created = self.orders.insert_pending(
                client_order_id=coid,
                signal_id=signal_id,
                risk_verdict_id=risk_verdict_id,
                venue=self.venue,
                symbol=symbol,
                side=side,
                qty=qty,
                mode=self._mode.value,
                ts=now,
            )
            if not created:
                self._suppress_duplicate(order, signal_id)
                return None
            self._link_decision(decision_id, order.id)
            self._audit(
                "ORDER_UPDATED",
                order,
                {
                    "client_order_id": coid,
                    "signal_id": signal_id,
                    "decision_audit_id": decision_id,
                    "from": None,
                    "to": "PENDING",
                    "side": side,
                    "qty": str(qty),
                },
                ts=now,
            )
            if approval_expires_ts is not None:
                self._audit(
                    APPROVAL_REQUIRED,
                    order,
                    {
                        "client_order_id": coid,
                        "signal_id": signal_id,
                        "symbol": symbol,
                        "side": side,
                        "qty": str(qty),
                        "expires_ts": approval_expires_ts,
                        "approve": f"augur orders approve {order.id} --confirm",
                    },
                    severity="WARN",
                    ts=now,
                )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return order

    # --- отправка -------------------------------------------------------------------

    def _signal_meta(self, signal_id: str) -> tuple[str, Decimal | None, Decimal | None]:
        row = self._conn.execute(
            "SELECT action, stop_loss, take_profit FROM signals WHERE id = ?", (signal_id,)
        ).fetchone()
        if row is None:
            return "BUY", None, None
        sl = None if row[1] is None else Decimal(str(row[1]))
        tp = None if row[2] is None else Decimal(str(row[2]))
        return str(row[0]), sl, tp

    def _request(self, order: OrderRecord) -> OrderRequest:
        action, sl, tp = self._signal_meta(order.signal_id)
        reduce_only = action not in _OPENING_ACTIONS
        return OrderRequest(
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            type=order.type,
            qty=order.qty,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            reduce_only=reduce_only,
            stop_loss=None if reduce_only else sl,
            take_profit=None if reduce_only else tp,
        )

    async def submit(self, order: OrderRecord) -> OrderRecord:
        """Отправить PENDING-ордер. Не-PENDING возвращается как есть (уже отправлен)."""
        if order.state != OrderState.PENDING:
            return order
        if awaiting_approval(self._conn, order.id):
            return order  # MICRO_LIVE: без ручного подтверждения не отправляется
        req = self._request(order)
        try:
            ack: OrderAck | None = await self._adapter.place_order(req)
        except RejectedError as e:
            ack = self.rejected_ack(order, str(e))
        except AdapterError as e:
            log.warning("place_order failed for %s: %s", order.client_order_id, type(e).__name__)
            ack = await self._recover(order, req)
        if ack is None:
            ack = OrderAck(
                client_order_id=order.client_order_id,
                venue_order_id=order.venue_order_id,
                state=OrderState.UNKNOWN,
            )
        rec = await self.apply_ack(order, ack)
        try:
            self._write_execution_result(rec)
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return rec

    @staticmethod
    def rejected_ack(order: OrderRecord, reason: str) -> OrderAck:
        return OrderAck(
            client_order_id=order.client_order_id,
            venue_order_id=order.venue_order_id,
            state=OrderState.REJECTED,
            reject_reason=reason[:500],
        )

    async def _recover(self, order: OrderRecord, req: OrderRequest) -> OrderAck | None:
        """ARCH §4.2: get_order → найден: он; «нет» — одна повторная отправка; иначе None."""
        try:
            found = await self._adapter.get_order(order.client_order_id, order.symbol)
        except AdapterError:
            return None  # статус не установить → UNKNOWN
        if found is not None:
            return found
        try:
            return await self._adapter.place_order(req)  # тот же client_order_id
        except RejectedError as e:
            return self.rejected_ack(order, str(e))
        except AdapterError:
            return None

    # --- применение ответа площадки ---------------------------------------------------

    def _new_fills(self, order: OrderRecord, ack: OrderAck) -> list[VenueFill]:
        if ack.fills:
            return list(ack.fills)
        if ack.filled_qty <= order.filled_qty or ack.avg_price is None:
            return []
        delta = ack.filled_qty - order.filled_qty
        prev_cost = order.filled_qty * (order.avg_fill_price or Decimal(0))
        price = ((ack.filled_qty * ack.avg_price - prev_cost) / delta).quantize(_Q)
        fee = max(ack.fee - order.fee, Decimal(0))
        fill_id = f"{ack.venue_order_id or order.client_order_id}:{ack.filled_qty}"
        return [VenueFill(venue_fill_id=fill_id, qty=delta, price=price, fee=fee, ts=self._clock())]

    async def apply_ack(
        self, order: OrderRecord, ack: OrderAck, *, actor: str = ACTOR, note: str | None = None
    ) -> OrderRecord:
        """Применить ответ площадки: fills (дедуп) → позиции → переходы → аудит. Одна транзакция."""
        events: list[FillEvent] = []
        try:
            path = path_to(order.state, ack.state)
        except IllegalTransitionError as e:
            if order.state in TERMINAL_STATES:
                return order  # повторный/запоздалый ответ по закрытому ордеру
            path = []
            try:
                self._audit(
                    "ORDER_UPDATED",
                    order,
                    {"error": str(e), "venue_state": ack.state.value},
                    severity="ERROR",
                    actor=actor,
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            return order
        _action, sl, tp = self._signal_meta(order.signal_id)
        opening = _action in _OPENING_ACTIONS
        now = self._clock()
        try:
            for f in self._new_fills(order, ack):
                rec = self.orders.insert_fill(
                    order.id,
                    venue_fill_id=f.venue_fill_id,
                    qty=f.qty,
                    price=f.price,
                    fee=f.fee,
                    ts=f.ts,
                )
                if rec is None:
                    continue
                eff = self.positions.apply_fill(
                    venue=order.venue,
                    symbol=order.symbol,
                    mode=order.mode,
                    side=order.side,
                    qty=f.qty,
                    price=f.price,
                    fee=f.fee,
                    ts=f.ts,
                    stop_loss=sl if opening else None,
                    take_profit=tp if opening else None,
                )
                ev = FillEvent(
                    order_id=order.id,
                    symbol=order.symbol,
                    side=order.side,
                    qty=f.qty,
                    price=f.price,
                    fee=f.fee,
                    realized_pnl=eff.realized_pnl,
                    trade_closed=eff.trade_closed,
                    ts=f.ts,
                )
                events.append(ev)
                self._audit(
                    "ORDER_UPDATED",
                    order,
                    {
                        "client_order_id": order.client_order_id,
                        "fill": {
                            "venue_fill_id": f.venue_fill_id,
                            "qty": str(f.qty),
                            "price": str(f.price),
                            "fee": str(f.fee),
                            "realized_pnl": str(eff.realized_pnl),
                            "trade_closed": eff.trade_closed,
                            "position_qty": str(eff.position.qty),
                        },
                    },
                    actor=actor,
                    ts=now,
                )
            fills = self.orders.fills(order.id)
            filled = sum((x.qty for x in fills), Decimal(0))
            fee = sum((x.fee for x in fills), Decimal(0))
            avg = (
                (sum((x.qty * x.price for x in fills), Decimal(0)) / filled).quantize(_Q)
                if filled > 0
                else None
            )
            if events and not path and order.state == OrderState.PARTIAL:
                path = [OrderState.PARTIAL]
            for state in path:
                before = order.state
                order = self.orders.update(
                    order,
                    state=state,
                    ts=now,
                    venue_order_id=ack.venue_order_id,
                    filled_qty=filled,
                    avg_fill_price=avg,
                    fee=fee,
                    fee_ccy=ack.fee_ccy,
                    reject_reason=ack.reject_reason if state == OrderState.REJECTED else None,
                )
                payload: dict[str, Any] = {
                    "client_order_id": order.client_order_id,
                    "from": before.value,
                    "to": state.value,
                    "filled_qty": str(filled),
                    "avg_fill_price": _j(avg),
                }
                if note:
                    payload["note"] = note
                if state == OrderState.REJECTED:
                    payload["reject_reason"] = order.reject_reason
                self._audit(
                    "ORDER_SUBMITTED" if state == OrderState.SUBMITTED else "ORDER_UPDATED",
                    order,
                    payload,
                    severity="WARN"
                    if state in (OrderState.REJECTED, OrderState.UNKNOWN)
                    else "INFO",
                    actor=actor,
                    ts=now,
                )
            if order.state == OrderState.FILLED:
                self._conn.execute(
                    "UPDATE signals SET status = 'EXECUTED' WHERE id = ? AND kind = 'final'",
                    (order.signal_id,),
                )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        if self.on_fill is not None:
            for ev in events:
                await self.on_fill(ev)
        return order

    async def on_venue_update(self, update: OrderAck) -> OrderRecord | None:
        """Обновление от площадки (стрим/поллинг). Неизвестный coid → None."""
        order = self.orders.get_by_coid(self.venue, update.client_order_id)
        if order is None:
            return None
        return await self.apply_ack(order, update)

    # --- отмена ---------------------------------------------------------------------

    async def cancel(self, order_id: str, *, reason: str, actor: str = ACTOR) -> OrderRecord:
        """Отменить ордер. PENDING, неизвестный площадке, → REJECTED (не был отправлен)."""
        order = self.orders.get(order_id)
        if order is None:
            raise KeyError(f"order not found: {order_id}")
        if order.state in TERMINAL_STATES:
            return order
        note = f"cancel: {reason}"
        if order.state in (OrderState.PENDING, OrderState.UNKNOWN):
            try:
                found = await self._adapter.get_order(order.client_order_id, order.symbol)
            except AdapterError:
                return order  # статус неизвестен — решит Reconciler
            if found is None:
                return await self.apply_ack(
                    order,
                    self.rejected_ack(order, f"cancelled before submit: {reason}"),
                    actor=actor,
                    note=note,
                )
            order = await self.apply_ack(order, found, actor=actor, note=note)
            if order.state in TERMINAL_STATES:
                return order
        try:
            ack = await self._adapter.cancel_order(order.client_order_id, order.symbol)
        except RejectedError:
            found = await self._adapter.get_order(order.client_order_id, order.symbol)
            if found is None:
                return order
            ack = found
        return await self.apply_ack(order, ack, actor=actor, note=note)

    async def cancel_all(self, *, reason: str, actor: str = ACTOR, entry_only: bool = False) -> int:
        """Отменить активные ордера режима. `entry_only` — только ордера на вход (BUY/SELL сигналы).

        Returns:
            Сколько ордеров перешло в CANCELLED/REJECTED.
        """
        n = 0
        for order in self.orders.active(mode=self._mode.value):
            if entry_only and self.orders.signal_action(order.signal_id) not in _OPENING_ACTIONS:
                continue
            rec = await self.cancel(order.id, reason=reason, actor=actor)
            if rec.state in (OrderState.CANCELLED, OrderState.REJECTED):
                n += 1
        return n

    async def expire_unapproved(self) -> list[str]:
        """Ордера, ждущие подтверждения дольше TTL сигнала → REJECTED (FUSION SPEC [Д7]).

        Returns:
            id отклонённых ордеров.
        """
        now = self._clock()
        out = []
        for order in self.orders.active(mode=self._mode.value):
            if order.state != OrderState.PENDING or not awaiting_approval(self._conn, order.id):
                continue
            row = self._conn.execute(
                "SELECT expires_ts FROM signals WHERE id = ?", (order.signal_id,)
            ).fetchone()
            if row is not None and row[0] is not None and now < int(row[0]):
                continue
            rec = await self.apply_ack(
                order,
                self.rejected_ack(order, "approval timeout: signal TTL expired"),
                actor="approval",
            )
            self._write_execution_result_commit(rec)
            out.append(rec.id)
        return out

    def _write_execution_result_commit(self, order: OrderRecord) -> None:
        try:
            self._write_execution_result(order)
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    async def expire_partials(self) -> int:
        """Отменить остаток PARTIAL MARKET-ордеров старше `partial_timeout_s` (ARCH §7.3)."""
        cutoff = self._clock() - self._settings.partial_timeout_s * 1000
        n = 0
        for order in self.orders.active(mode=self._mode.value):
            if (
                order.state == OrderState.PARTIAL
                and order.type == "MARKET"
                and order.created_ts <= cutoff
            ):
                rec = await self.cancel(order.id, reason="partial_timeout")
                n += rec.state == OrderState.CANCELLED
        return n

    # --- системные выходы (снижают риск) -----------------------------------------------

    def _system_signal_and_decision(
        self, symbol: str, *, leg: str, reason: str, now: int
    ) -> tuple[str, str]:
        signal_id = new_ulid(now)
        self._conn.execute(
            "INSERT INTO signals(id, kind, parent_ids_json, symbol, action, strength, source, "
            "risk_flags_json, config_hash, code_version, created_ts, expires_ts, status) "
            "VALUES (?, 'final', '[]', ?, 'CLOSE', 1.0, 'classic', '[]', ?, ?, ?, ?, 'APPROVED')",
            (signal_id, symbol, self._config_hash, self._app_version, now, now + _SYSTEM_TTL_MS),
        )
        rec = DecisionAuditRecord(
            id=new_ulid(now),
            signal_id=signal_id,
            fused_signal_id=None,
            app_version=self._app_version,
            strategy_version=f"system:{leg}",
            ml_model_version=NO_ARTIFACT,
            llm_prompt_version=NO_ARTIFACT,
            ml_prediction_id=None,
            llm_status=None,
            risk_verdict="APPROVED",
            risk_reason=f"{reason}; risk-reducing system exit, Risk Gate bypassed (ARCH §7.4)",
            config_hash=self._config_hash,
            mode=self._mode.value,
            created_at=now,
        )
        assert_auditable(rec)
        self._conn.execute(
            "INSERT INTO decision_audit(id, signal_id, fused_signal_id, app_version, "
            "strategy_version, ml_model_version, llm_prompt_version, ml_prediction_id, llm_status, "
            "risk_verdict, risk_reason, config_hash, mode, created_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)",
            (
                rec.id,
                rec.signal_id,
                rec.app_version,
                rec.strategy_version,
                rec.ml_model_version,
                rec.llm_prompt_version,
                rec.risk_verdict,
                rec.risk_reason,
                rec.config_hash,
                rec.mode,
                rec.created_at,
            ),
        )
        return signal_id, rec.id

    def _has_active_exit(self, symbol: str) -> bool:
        return any(
            self.orders.signal_action(o.signal_id) == "CLOSE"
            for o in self.orders.active(mode=self._mode.value, symbol=symbol)
        )

    async def close_position(
        self, symbol: str, *, leg: Literal["sl", "tp", "flatten"], reason: str, actor: str = ACTOR
    ) -> OrderRecord | None:
        """Закрыть позицию рыночным reduce_only-ордером мимо Risk Gate (системный выход)."""
        pos = self.positions.get(self.venue, symbol, self._mode.value)
        if pos is None or pos.qty == 0 or self._has_active_exit(symbol):
            return None
        now = self._clock()
        try:
            signal_id, decision_id = self._system_signal_and_decision(
                symbol, leg=leg, reason=reason, now=now
            )
            order, _created = self.orders.insert_pending(
                client_order_id=make_client_order_id(signal_id, leg, 0),
                signal_id=signal_id,
                risk_verdict_id=decision_id,
                venue=self.venue,
                symbol=symbol,
                side="SELL" if pos.qty > 0 else "BUY",
                qty=abs(pos.qty),
                mode=self._mode.value,
                ts=now,
            )
            self._link_decision(decision_id, order.id)
            self._audit(
                "ORDER_UPDATED",
                order,
                {
                    "client_order_id": order.client_order_id,
                    "signal_id": signal_id,
                    "decision_audit_id": decision_id,
                    "from": None,
                    "to": "PENDING",
                    "system_exit": leg,
                    "reason": reason,
                },
                actor=actor,
                ts=now,
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return await self.submit(order)

    async def flatten(self, *, reason: str, actor: str = ACTOR) -> list[OrderRecord]:
        """Закрыть все открытые позиции режима (kill switch / `cb.flatten_on_trip`)."""
        out = []
        for pos in self.positions.open(self._mode.value, venue=self.venue):
            rec = await self.close_position(pos.symbol, leg="flatten", reason=reason, actor=actor)
            if rec is not None:
                out.append(rec)
        return out

    async def check_protective_exits(
        self, prices: dict[str, Decimal] | None = None
    ) -> list[OrderRecord]:
        """SL/TP по открытым позициям. Работает всегда — и при CB TRIPPED, и при KILLED.

        Args:
            prices: последняя цена по символу; нет цены — запрос `get_latest` (mid).
        """
        out = []
        for pos in self.positions.open(self._mode.value, venue=self.venue):
            if pos.stop_loss is None and pos.take_profit is None:
                continue
            price = (prices or {}).get(pos.symbol)
            if price is None:
                tick = await self._adapter.get_latest(pos.symbol)
                price = tick.last if tick.last is not None else (tick.bid + tick.ask) / 2
            long = pos.qty > 0
            leg: Literal["sl", "tp"] | None = None
            if pos.stop_loss is not None and (
                price <= pos.stop_loss if long else price >= pos.stop_loss
            ):
                leg = "sl"
            elif pos.take_profit is not None and (
                price >= pos.take_profit if long else price <= pos.take_profit
            ):
                leg = "tp"
            if leg is None:
                continue
            rec = await self.close_position(
                pos.symbol, leg=leg, reason=f"{leg} hit at {price}", actor="protective_exit"
            )
            if rec is not None:
                out.append(rec)
        return out

    # --- портфель -------------------------------------------------------------------

    async def snapshot(self) -> PortfolioSnapshot:
        """Снимок портфеля по счёту площадки → `portfolio_snapshots` (COMMIT)."""
        account = await self._adapter.get_account_state()
        try:
            snap = self.tracker.snapshot(
                self._mode.value, account, self.positions.all(self._mode.value), ts=self._clock()
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return snap


def restore_paper_broker(
    broker: PaperAdapter, conn: sqlite3.Connection, *, mode: TradingMode, start_cash: Decimal
) -> None:
    """Восстановить состояние PaperAdapter из SQLite после рестарта (позиции + кэш снимка)."""
    rows = PositionManager(conn).open(mode.value, venue=broker.venue)
    snap = PortfolioTracker(conn).latest(mode.value)
    broker.restore(
        cash=snap.cash if snap is not None else start_cash,
        positions=[VenuePosition(symbol=p.symbol, qty=p.qty, avg_price=p.avg_price) for p in rows],
    )
