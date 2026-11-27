"""Хранилище режима `mode_state` (ARCHITECTURE.md §1.4, §7.1) — минимум для 0.6.0.

Здесь только то, что нужно CB и kill switch: чтение режима и ПОНИЖЕНИЕ без кода
(ARCH §1.4: «понижение разрешено без кода подтверждения»), плюс установка PAPER после
`cb reset`. Повышение режима с одноразовым кодом (`ModeController.request_change` /
`confirm_change`, promotion-критерии) — этап 0.7.0 (MICRO_LIVE).

Каждая смена — `mode_state` + `audit_log: MODE_CHANGED` в одной транзакции вызывающего.
"""

import sqlite3

from augur.config.settings import TradingMode
from augur.db.audit import append_audit
from augur.risk.context import MODE_ORDER

__all__ = ["ModeStore", "mode_rank"]


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
        """Режим из `mode_state`, но не выше конфига; нет строки — режим из конфига."""
        stored = self.stored()
        if stored is None:
            return self._configured
        return min(stored, self._configured, key=mode_rank)

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
            payload={"from": before.value, "to": target.value, "reason": reason},
            ts=ts,
        )
        return True
