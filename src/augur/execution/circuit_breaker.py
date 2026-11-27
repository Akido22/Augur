"""Circuit breaker (ARCHITECTURE.md §7.4).

Состояние — `cb_state` (персистентно, переживает рестарт); нет строки — `ARMED`.

`trip(trigger, detail)`:
1) `cb_state = TRIPPED` (persist); 2) Risk Gate R2 блокирует новые входы;
3) `cancel_all` открытых ордеров НА ВХОД; 4) режим → PAPER-hold (позиции не закрываются,
   если не включено `cb.flatten_on_trip`); 5) `audit_log: CB_TRIPPED` (CRITICAL);
6) CRITICAL-нотификация — модуль Notify ещё не реализован, пока `logging.critical`.
SL/TP и закрытие позиций при TRIPPED продолжают работать.

`reset(actor, confirm_code)` — только с одноразовым кодом (`issue_reset_code`, TTL
`cb.reset_code_ttl_s`), только из CLI. После reset режим = PAPER; повторная эскалация —
через ModeController (0.7.0). Автоматического сброса нет. KILLED сбрасывается тем же reset.
"""

import logging
import secrets
import sqlite3
from collections import deque
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from augur.adapters.base import FatalAdapterError
from augur.config.settings import (
    CircuitBreakerSettings,
    FusionSettings,
    RiskLimits,
    TradingMode,
)
from augur.db.audit import append_audit
from augur.execution.modes import ModeStore
from augur.execution.order_manager import OrderManager
from augur.risk.post_trade import CBTrigger, RiskMetrics, TriggerHit, evaluate_triggers

__all__ = ["CBStatus", "CircuitBreaker", "InvalidResetCodeError", "ResetTicket"]

log = logging.getLogger(__name__)

CBStateName = Literal["ARMED", "TRIPPED", "KILLED"]


class InvalidResetCodeError(PermissionError):
    """Код подтверждения неверен, истёк или не выдавался."""


class CBStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: CBStateName
    reason: str | None
    tripped_ts: int | None
    reset_by: str | None
    reset_ts: int | None


class ResetTicket(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str  # 6 цифр
    expires_ts: int


class CircuitBreaker:
    """Args:
    conn: соединение (0005+).
    orders: OrderManager режима (отмена входов, flatten).
    modes: хранилище режима.
    limits / cb / fusion: пороги триггеров (конфиг).
    clock: «сейчас», Unix ms UTC.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        orders: OrderManager | None,
        modes: ModeStore,
        limits: RiskLimits,
        cb: CircuitBreakerSettings,
        fusion: FusionSettings,
        clock: Callable[[], int],
    ) -> None:
        self._conn = conn
        self._orders = orders
        self._modes = modes
        self._limits = limits
        self._cb = cb
        self._fusion = fusion
        self._clock = clock
        self._errors: deque[int] = deque()
        self._ticket: ResetTicket | None = None
        self._pending: TriggerHit | None = None

    # --- состояние ------------------------------------------------------------------

    def status(self) -> CBStatus:
        row = self._conn.execute(
            "SELECT state, reason, tripped_ts, reset_by, reset_ts FROM cb_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return CBStatus(
                state="ARMED", reason=None, tripped_ts=None, reset_by=None, reset_ts=None
            )
        state = str(row[0])
        return CBStatus(
            state="ARMED" if state == "ARMED" else ("KILLED" if state == "KILLED" else "TRIPPED"),
            reason=row[1],
            tripped_ts=row[2],
            reset_by=row[3],
            reset_ts=row[4],
        )

    @property
    def state(self) -> CBStateName:
        return self.status().state

    def _write_state(self, state: CBStateName, reason: str | None, ts: int) -> None:
        self._conn.execute(
            "INSERT INTO cb_state(id, state, reason, tripped_ts) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET state = excluded.state, reason = excluded.reason, "
            "tripped_ts = excluded.tripped_ts",
            (state, reason, ts),
        )

    # --- срабатывание ---------------------------------------------------------------

    async def evaluate(self, metrics: RiskMetrics) -> TriggerHit | None:
        """Проверить триггеры; первый сработавший → `trip`. Уже TRIPPED/KILLED → None."""
        if self.state != "ARMED":
            return None
        hits = evaluate_triggers(metrics, limits=self._limits, cb=self._cb, fusion=self._fusion)
        if not hits:
            return None
        await self.trip(hits[0].trigger, hits[0].detail, extra=[h.trigger.value for h in hits])
        return hits[0]

    def record_adapter_error(self, err: BaseException) -> None:
        """Хук `ResilientAdapter.on_error` (синхронный): копит ошибки в окне. Порог или
        `FatalAdapterError` → отложенный триггер, его исполняет `check_adapter_errors()`."""
        now = self._clock()
        if isinstance(err, FatalAdapterError):
            self._pending = TriggerHit(
                trigger=CBTrigger.ADAPTER_ERRORS, detail=f"fatal: {type(err).__name__}"
            )
            return
        self._errors.append(now)
        window = self._cb.adapter_error_window_s * 1000
        while self._errors and self._errors[0] < now - window:
            self._errors.popleft()
        if len(self._errors) >= self._cb.adapter_error_max:
            self._pending = TriggerHit(
                trigger=CBTrigger.ADAPTER_ERRORS,
                detail=f"{len(self._errors)} adapter errors in {self._cb.adapter_error_window_s}s",
            )

    async def check_adapter_errors(self) -> TriggerHit | None:
        """Сработать по накопленным ошибкам адаптера (вызывается после операций с площадкой)."""
        hit, self._pending = self._pending, None
        if hit is None or self.state != "ARMED":
            return None
        await self.trip(hit.trigger, hit.detail)
        return hit

    async def trip(
        self,
        trigger: CBTrigger,
        detail: str,
        *,
        actor: str = "circuit_breaker",
        extra: list[str] | None = None,
    ) -> bool:
        """TRIPPED + отмена входов + PAPER-hold + аудит. Уже TRIPPED/KILLED → False."""
        if self.state != "ARMED":
            return False
        now = self._clock()
        reason = f"{trigger.value}: {detail}"
        try:
            self._write_state("TRIPPED", reason, now)
            if self._modes.current() in (TradingMode.MICRO_LIVE, TradingMode.FULL_AUTO):
                self._modes.set_mode(  # PAPER-hold: понижение, позиции не трогаем
                    TradingMode.PAPER, actor=actor, reason=f"cb trip: {reason}", ts=now
                )
            append_audit(
                self._conn,
                actor=actor,
                event="CB_TRIPPED",
                severity="CRITICAL",
                ref_type="cb",
                ref_id=trigger.value,
                payload={
                    "trigger": trigger.value,
                    "detail": detail,
                    "all_triggers": extra or [trigger.value],
                },
                ts=now,
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        log.critical(
            "CIRCUIT BREAKER TRIPPED: %s", reason
        )  # Notify — отдельный модуль (ARCH §2.11)
        if self._orders is not None:
            await self._orders.cancel_all(
                reason=f"cb trip: {trigger.value}", actor=actor, entry_only=True
            )
            if self._cb.flatten_on_trip:
                await self._orders.flatten(reason=f"cb trip: {trigger.value}", actor=actor)
        return True

    # --- сброс ----------------------------------------------------------------------

    def issue_reset_code(self) -> ResetTicket:
        """Одноразовый 6-значный код (TTL `cb.reset_code_ttl_s`). Хранится только в памяти."""
        code = f"{secrets.randbelow(1_000_000):06d}"
        self._ticket = ResetTicket(
            code=code, expires_ts=self._clock() + self._cb.reset_code_ttl_s * 1000
        )
        return self._ticket

    def reset(self, *, actor: str, confirm_code: str) -> CBStatus:
        """ARMED + режим PAPER + `CB_RESET`. Код одноразовый: после попытки он сгорает.

        Raises:
            InvalidResetCodeError: нет кода, истёк или не совпал (состояние не меняется).
        """
        ticket, self._ticket = self._ticket, None
        now = self._clock()
        if (
            ticket is None
            or now > ticket.expires_ts
            or not secrets.compare_digest(ticket.code, confirm_code)
        ):
            raise InvalidResetCodeError("invalid or expired confirmation code")
        before = self.status()
        if before.state == "ARMED":
            return before
        try:
            self._conn.execute(
                "UPDATE cb_state SET state = 'ARMED', reset_by = ?, reset_ts = ? WHERE id = 1",
                (actor, now),
            )
            self._modes.set_mode(TradingMode.PAPER, actor=actor, reason="cb reset", ts=now)
            append_audit(
                self._conn,
                actor=actor,
                event="CB_RESET",
                severity="WARN",
                ref_type="cb",
                ref_id=before.state,
                payload={"from": before.state, "reason": before.reason, "mode_after": "PAPER"},
                ts=now,
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        self._errors.clear()
        return self.status()
