"""Strategy Framework: базовый класс, сигнал, реестр, стратегии (ARCHITECTURE.md §2.5)."""

from augur.strategies.base import Signal, SignalAction, Strategy, StrategyParamsError
from augur.strategies.registry import (
    UnknownStrategyError,
    get_strategy,
    list_strategies,
    register_strategy,
)

# Импорт ради регистрации в реестре.
from augur.strategies.rsi import RsiStrategy  # noqa: E402
from augur.strategies.sma_cross import SmaCross  # noqa: E402

__all__ = [
    "RsiStrategy",
    "Signal",
    "SignalAction",
    "SmaCross",
    "Strategy",
    "StrategyParamsError",
    "UnknownStrategyError",
    "get_strategy",
    "list_strategies",
    "register_strategy",
]
