"""Команды интерфейсов, меняющие состояние (ARCHITECTURE.md §7.4, §9).

В 0.9.0 из GUI/REST доступна только одна команда — kill switch (безопасное направление,
без кода подтверждения по ARCH §7.4). CLI `augur kill-switch` и `POST /api/v1/kill-switch`
вызывают одну и ту же функцию. Approve первого ордера, смена режима и снятие паузы новостей
остаются в CLI с кодом / `--confirm`.
"""

import sqlite3
from collections.abc import Callable

from augur import __version__
from augur.adapters.base import MarketAdapter
from augur.config.settings import Settings, TradingMode
from augur.execution import KillSwitch, ModeStore, build_live_stack, build_paper_stack
from augur.execution.kill_switch import KillReport
from augur.execution.modes import mode_rank
from augur.interfaces.dto import KillSwitchResultDTO

__all__ = ["engage_kill_switch", "live_allowed"]


def live_allowed(settings: Settings) -> bool:
    """Конфиг разрешает реальное исполнение (MICRO_LIVE и выше)."""
    return mode_rank(settings.trading_mode) >= mode_rank(TradingMode.MICRO_LIVE) and mode_rank(
        settings.max_allowed_mode
    ) >= mode_rank(TradingMode.MICRO_LIVE)


def _state_only_switch(
    conn: sqlite3.Connection, settings: Settings | None, clock: Callable[[], int]
) -> KillSwitch:
    configured = settings.trading_mode if settings is not None else TradingMode.PAPER
    return KillSwitch(
        conn, orders=None, modes=ModeStore(conn, configured_mode=configured), clock=clock
    )


async def engage_kill_switch(
    conn: sqlite3.Connection,
    settings: Settings | None,
    *,
    actor: str,
    flatten: bool,
    clock: Callable[[], int],
    live_adapter: MarketAdapter | None = None,
) -> KillSwitchResultDTO:
    """KILLED, режим ANALYSIS_ONLY, отмена ордеров, при `flatten` — закрытие позиций.

    Состояние KILLED выставляется всегда, даже если площадка недоступна или настройки не
    загружены; тогда ордера не трогаются, а причина попадает в `warnings`.

    Args:
        conn: соединение с БД (запись).
        settings: настройки; None — не загрузились (только состояние и режим).
        actor: автор для `audit_log` (`cli:<user>` / `api:<token_id>`).
        flatten: закрыть позиции reduce_only-ордерами.
        clock: «сейчас», Unix ms UTC.
        live_adapter: реальный адаптер для MICRO_LIVE; None — CcxtAdapter из настроек.
    """
    warnings: list[str] = []
    report: KillReport
    if settings is not None and live_allowed(settings):
        try:
            stack = build_live_stack(
                conn, settings, app_version=__version__, clock=clock, adapter=live_adapter
            )
            await stack.venue_adapter.connect()
        except Exception as e:  # noqa: BLE001 — остановка важнее причины сбоя связи
            warnings.append(
                f"venue unavailable ({type(e).__name__}): KILLED is set, but orders and "
                "positions at the venue are NOT touched — cancel/close them manually NOW"
            )
            report = await _state_only_switch(conn, settings, clock).engage(
                actor=actor, flatten=flatten
            )
        else:
            try:
                report = await stack.kill_switch.engage(actor=actor, flatten=flatten)
            finally:
                await stack.venue_adapter.close()
    elif settings is not None and settings.trading_mode == TradingMode.PAPER:
        switch = build_paper_stack(conn, settings, app_version=__version__, clock=clock).kill_switch
        report = await switch.engage(actor=actor, flatten=flatten)
    else:
        reason = "settings not loaded" if settings is None else f"mode {settings.trading_mode}"
        warnings.append(f"{reason}: state/mode only, orders not cancelled")
        report = await _state_only_switch(conn, settings, clock).engage(
            actor=actor, flatten=flatten
        )
    return KillSwitchResultDTO(
        was_state=report.was_state,
        cancelled=report.cancelled,
        flatten_orders=len(report.flatten_orders),
        warnings=warnings,
    )
