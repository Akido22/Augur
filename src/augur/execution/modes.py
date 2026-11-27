"""Режим работы: `mode_state` и смена режима (ARCHITECTURE.md §1.4, §7.1).

`ModeStore` — чтение режима и ПОНИЖЕНИЕ без кода (ARCH §1.4), плюс PAPER после `cb reset`.
Им пользуются CB и kill switch.

`ModeController` (0.7.0) — повышение режима. Условия (ARCH §1.4, §7.1), все сразу:
1) цель ≤ `settings.trading_mode` и ≤ `settings.max_allowed_mode`;
2) ровно один шаг вверх `ANALYSIS_ONLY → SIGNAL_ONLY → PAPER → MICRO_LIVE → FULL_AUTO`;
3) CB в ARMED (не TRIPPED/KILLED);
4) для FULL_AUTO — promotion: в текущем пребывании в MICRO_LIVE исполнено не менее
   `mode.min_micro_trades` ордеров и CB не срабатывал;
5) для MICRO_LIVE — preflight (сверка позиций с реальной площадкой), если он передан.
`request_change` выдаёт тикет с одноразовым 6-значным кодом (TTL `mode.code_ttl_s`, только в
памяти процесса) и пишет `MODE_CHANGE_REQUESTED`. `confirm_change` повторно проверяет
условия и атомарно пишет `mode_state` + `MODE_CHANGED`; при ошибке режим не меняется.
Код обязателен для MICRO_LIVE/FULL_AUTO; CLI подтверждает переходы до PAPER сам.

Каждая смена — `mode_state` + `audit_log: MODE_CHANGED` в одной транзакции. Вход в режим
сбрасывает `micro_first_order_confirmed` (первый ордер MICRO_LIVE снова ждёт подтверждения).
"""

import secrets
import sqlite3
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict

from augur.config.settings import Settings, TradingMode
from augur.db.audit import append_audit
from augur.ids import new_ulid
from augur.risk.context import MODE_ORDER

__all__ = [
    "InvalidModeCodeError",
    "ModeChangeError",
    "ModeChangeTicket",
    "ModeController",
    "ModeStore",
    "PromotionStatus",
    "mode_rank",
]

CODE_REQUIRED = frozenset({TradingMode.MICRO_LIVE, TradingMode.FULL_AUTO})


def mode_rank(mode: TradingMode) -> int:
    return MODE_ORDER.index(mode)


class ModeStore:
    """Args: conn — соединение; configured_mode — `settings.trading_mode` (потолок режима)."""

    def __init__(self, conn: sqlite3.Connection, *, configured_mode: TradingMode) -> None:
        self._conn = conn
        self._configured = configured_mode

    def stored(self) -> TradingMode | None:
        row = self._conn.execute("SELECT mode FROM mode_state WHERE id = 1").fetchone()
        if row is None:
            return None
        try:
            return TradingMode(row[0])
        except ValueError:
            return TradingMode.ANALYSIS_ONLY

    def current(self) -> TradingMode:
        """Режим из `mode_state`, но не выше конфига.

        Нет строки — режим из конфига, но не выше PAPER: MICRO_LIVE/FULL_AUTO включаются только
        через `ModeController` (ARCH §1.4), конфиг лишь разрешает их.
        """
        return self.effective(self.stored(), self._configured)

    @staticmethod
    def effective(stored: TradingMode | None, configured: TradingMode) -> TradingMode:
        if stored is None:
            return min(configured, TradingMode.PAPER, key=mode_rank)
        return min(stored, configured, key=mode_rank)

    def set_mode(self, target: TradingMode, *, actor: str, reason: str, ts: int) -> bool:
        """Записать режим (без COMMIT). Повышение здесь запрещено, кроме ≤ PAPER после reset.

        Returns:
            True — режим изменён; False — уже такой.

        Raises:
            PermissionError: попытка поднять режим выше PAPER (только через ModeController, 0.7.0).
        """
        before = self.current()
        if mode_rank(target) > mode_rank(before) and mode_rank(target) > mode_rank(
            TradingMode.PAPER
        ):
            raise PermissionError(f"mode escalation {before} -> {target} requires ModeController")
        target = min(target, self._configured, key=mode_rank)
        return self.write(target, actor=actor, reason=reason, ts=ts, before=before)

    def write(
        self,
        target: TradingMode,
        *,
        actor: str,
        reason: str,
        ts: int,
        before: TradingMode | None = None,
        extra: dict[str, object] | None = None,
    ) -> bool:
        """`mode_state` + `MODE_CHANGED` без проверок и без COMMIT. Только для ModeController
        и `set_mode`: повышение режима в обход ModeController запрещено.
        """
        before = before if before is not None else self.current()
        if self.stored() == target:
            return False
        self._conn.execute(
            "INSERT INTO mode_state(id, mode, changed_ts, changed_by, micro_first_order_confirmed) "
            "VALUES (1, ?, ?, ?, 0) ON CONFLICT(id) DO UPDATE SET mode = excluded.mode, "
            "changed_ts = excluded.changed_ts, changed_by = excluded.changed_by, "
            "micro_first_order_confirmed = 0",
            (target.value, ts, actor),
        )
        append_audit(
            self._conn,
            actor=actor,
            event="MODE_CHANGED",
            severity="WARN",
            ref_type="mode",
            ref_id=target.value,
            payload={"from": before.value, "to": target.value, "reason": reason, **(extra or {})},
            ts=ts,
        )
        return True


class ModeChangeError(PermissionError):
    """Смена режима запрещена (условия ARCH §1.4 / §7.1 не выполнены)."""


class InvalidModeCodeError(ModeChangeError):
    """Неверный, просроченный или уже использованный код подтверждения."""


class ModeChangeTicket(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    source: TradingMode
    target: TradingMode
    code: str  # 6 цифр; в аудит не пишется
    code_required: bool
    expires_ts: int


class PromotionStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    micro_trades: int
    required: int
    cb_tripped_since_entry: bool

    @property
    def ok(self) -> bool:
        return self.micro_trades >= self.required and not self.cb_tripped_since_entry


Preflight = Callable[[TradingMode], Awaitable[None]]


class ModeController:
    """Args:
    conn: соединение.
    settings: `trading_mode`, `max_allowed_mode`, `mode` (TTL кода, promotion).
    clock: «сейчас», Unix ms UTC.
    preflight: проверка перед входом в MICRO_LIVE (сверка позиций с площадкой); бросает
        исключение, если входить нельзя.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: Settings,
        clock: Callable[[], int],
        preflight: Preflight | None = None,
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._clock = clock
        self._preflight = preflight
        self.store = ModeStore(conn, configured_mode=settings.trading_mode)
        self._tickets: dict[str, ModeChangeTicket] = {}

    def current(self) -> TradingMode:
        return self.store.current()

    def sync_with_config(self, *, actor: str = "system") -> TradingMode:
        """При старте (ARCH §7.1): сохранённый режим выше конфига → понижение с аудитом."""
        stored = self.store.stored()
        configured = self._settings.trading_mode
        if stored is not None and mode_rank(stored) > mode_rank(configured):
            self._commit(
                lambda: self.store.write(
                    configured,
                    actor=actor,
                    reason="config ceiling (settings.trading_mode)",
                    ts=self._clock(),
                    before=stored,
                )
            )
        return self.current()

    def _cb_state(self) -> str:
        row = self._conn.execute("SELECT state FROM cb_state WHERE id = 1").fetchone()
        return "ARMED" if row is None else str(row[0])

    def promotion_status(self) -> PromotionStatus:
        """Promotion в FULL_AUTO: исполненные ордера MICRO_LIVE с момента входа в режим."""
        required = self._settings.mode.min_micro_trades
        row = self._conn.execute("SELECT mode, changed_ts FROM mode_state WHERE id = 1").fetchone()
        if row is None or row[0] != TradingMode.MICRO_LIVE.value:
            return PromotionStatus(micro_trades=0, required=required, cb_tripped_since_entry=False)
        since = int(row[1])
        trades = self._conn.execute(
            "SELECT COUNT(*) FROM orders WHERE mode = 'MICRO_LIVE' AND state = 'FILLED' "
            "AND created_ts >= ?",
            (since,),
        ).fetchone()[0]
        tripped = self._conn.execute(
            "SELECT 1 FROM audit_log WHERE event IN ('CB_TRIPPED', 'KILL_SWITCH') AND ts >= ? "
            "LIMIT 1",
            (since,),
        ).fetchone()
        return PromotionStatus(
            micro_trades=int(trades), required=required, cb_tripped_since_entry=tripped is not None
        )

    def check_escalation(self, target: TradingMode) -> TradingMode:
        """Проверить условия повышения; вернуть текущий режим.

        Raises:
            ModeChangeError: любое условие не выполнено.
        """
        source = self.current()
        if mode_rank(target) <= mode_rank(source):
            raise ModeChangeError(f"{source} -> {target} is not an escalation (use downgrade)")
        if mode_rank(target) != mode_rank(source) + 1:
            raise ModeChangeError(f"{source} -> {target}: escalation is one step at a time")
        for name, ceiling in (
            ("trading_mode", self._settings.trading_mode),
            ("max_allowed_mode", self._settings.max_allowed_mode),
        ):
            if mode_rank(target) > mode_rank(ceiling):
                raise ModeChangeError(f"{target} above settings.{name}={ceiling}")
        cb = self._cb_state()
        if cb != "ARMED":
            raise ModeChangeError(f"circuit breaker {cb}: run augur cb reset --confirm first")
        if target == TradingMode.FULL_AUTO:
            st = self.promotion_status()
            if not st.ok:
                raise ModeChangeError(
                    f"FULL_AUTO promotion not met: {st.micro_trades}/{st.required} MICRO_LIVE "
                    f"trades, cb_tripped_since_entry={st.cb_tripped_since_entry}"
                )
        return source

    async def request_change(self, target: TradingMode, *, actor: str) -> ModeChangeTicket:
        """Проверки + preflight → тикет с одноразовым кодом, `MODE_CHANGE_REQUESTED` (COMMIT)."""
        source = self.check_escalation(target)
        if target == TradingMode.MICRO_LIVE and self._preflight is not None:
            await self._preflight(target)
        now = self._clock()
        ticket = ModeChangeTicket(
            ticket_id=new_ulid(now),
            source=source,
            target=target,
            code=f"{secrets.randbelow(1_000_000):06d}",
            code_required=target in CODE_REQUIRED,
            expires_ts=now + self._settings.mode.code_ttl_s * 1000,
        )
        self._commit(
            lambda: append_audit(
                self._conn,
                actor=actor,
                event="MODE_CHANGE_REQUESTED",
                severity="WARN",
                ref_type="mode",
                ref_id=target.value,
                payload={
                    "from": source.value,
                    "to": target.value,
                    "ticket_id": ticket.ticket_id,
                    "expires_ts": ticket.expires_ts,
                },
                ts=now,
            )
        )
        self._tickets[ticket.ticket_id] = ticket
        return ticket

    def confirm_change(self, ticket_id: str, code: str, *, actor: str) -> TradingMode:
        """Код верен и не просрочен → повторная проверка → `mode_state` + `MODE_CHANGED`.

        Raises:
            InvalidModeCodeError: тикет не найден, просрочен или код неверен (тикет сгорает).
            ModeChangeError: условия перестали выполняться (например, CB сработал).
        """
        ticket = self._tickets.pop(ticket_id, None)  # одноразовый: любая попытка сжигает
        now = self._clock()
        if (
            ticket is None
            or now > ticket.expires_ts
            or not secrets.compare_digest(ticket.code, code)
        ):
            raise InvalidModeCodeError("invalid or expired confirmation code")
        source = self.check_escalation(ticket.target)
        if source != ticket.source:
            raise ModeChangeError(f"mode changed since request: {ticket.source} -> {source}")
        self._commit(
            lambda: self.store.write(
                ticket.target,
                actor=actor,
                reason="mode set --confirm",
                ts=now,
                before=source,
                extra={"ticket_id": ticket.ticket_id},
            )
        )
        return self.current()

    def downgrade(self, target: TradingMode, *, actor: str, reason: str) -> bool:
        """Понижение без кода (ARCH §1.4). Цель не ниже текущего → `ModeChangeError`."""
        if mode_rank(target) >= mode_rank(self.current()):
            raise ModeChangeError(f"{self.current()} -> {target} is not a downgrade")
        return self._commit(
            lambda: self.store.set_mode(target, actor=actor, reason=reason, ts=self._clock())
        )

    def _commit(self, fn: Callable[[], object]) -> bool:
        try:
            out = fn()
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return bool(out)
