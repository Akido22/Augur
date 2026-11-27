"""Kill switch (ARCHITECTURE.md §7.4).

`engage(actor, flatten=True)` немедленно и без кода подтверждения (скорость важнее):
1) `cb_state = KILLED` (персистентно — переживает рестарт; Risk Gate R2 блокирует всё,
   включая CLOSE/REDUCE по сигналам); 2) режим → ANALYSIS_ONLY; 3) `audit_log: KILL_SWITCH`
   (CRITICAL) — шаги 1–3 одной транзакцией; 4) `cancel_all`; 5) при `flatten=True` — закрыть
   все позиции рыночными reduce_only-ордерами через адаптер, минуя сигнальный контур, с аудитом.

Снятие — только `augur cb reset --confirm` с одноразовым кодом → режим PAPER
(`CircuitBreaker.reset`). Автоматического снятия нет.
"""

import logging
import sqlite3
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from augur.config.settings import TradingMode
from augur.db.audit import append_audit
from augur.execution.modes import ModeStore
from augur.execution.order_manager import OrderManager

__all__ = ["KillReport", "KillSwitch"]

log = logging.getLogger(__name__)


class KillReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    was_state: str
    cancelled: int
    flatten_orders: list[str]  # id ордеров на закрытие
    flatten_requested: bool
    ts: int


class KillSwitch:
    """Args:
    conn: соединение.
    orders: OrderManager режима; None — только состояние и режим (без отмен и flatten).
    modes: хранилище режима.
    clock: «сейчас», Unix ms UTC.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        orders: OrderManager | None,
        modes: ModeStore,
        clock: Callable[[], int],
    ) -> None:
        self._conn = conn
        self._orders = orders
        self._modes = modes
        self._clock = clock

    def _state(self) -> str:
        row = self._conn.execute("SELECT state FROM cb_state WHERE id = 1").fetchone()
        return "ARMED" if row is None else str(row[0])

    async def engage(self, *, actor: str, flatten: bool = True) -> KillReport:
        now = self._clock()
        was = self._state()
        try:
            self._conn.execute(
                "INSERT INTO cb_state(id, state, reason, tripped_ts) VALUES (1, 'KILLED', ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET state = 'KILLED', reason = excluded.reason, "
                "tripped_ts = excluded.tripped_ts",
                (f"kill switch by {actor}", now),
            )
            self._modes.set_mode(
                TradingMode.ANALYSIS_ONLY, actor=actor, reason="kill switch", ts=now
            )
            append_audit(
                self._conn,
                actor=actor,
                event="KILL_SWITCH",
                severity="CRITICAL",
                ref_type="cb",
                ref_id="KILLED",
                payload={"from": was, "flatten": flatten},
                ts=now,
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        log.critical("KILL SWITCH ENGAGED by %s (flatten=%s)", actor, flatten)
        cancelled = 0
        closing: list[str] = []
        if self._orders is not None:
            cancelled = await self._orders.cancel_all(reason="kill switch", actor=actor)
            if flatten:
                closing = [
                    o.id for o in await self._orders.flatten(reason="kill switch", actor=actor)
                ]
        return KillReport(
            was_state=was,
            cancelled=cancelled,
            flatten_orders=closing,
            flatten_requested=flatten,
            ts=now,
        )
