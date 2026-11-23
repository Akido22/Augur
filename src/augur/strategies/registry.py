"""Реестр стратегий: имя → класс (ARCHITECTURE.md §2.5)."""

from typing import Any, TypeVar

from augur.strategies.base import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}

S = TypeVar("S", bound=type[Strategy])


class UnknownStrategyError(KeyError):
    """Стратегия с таким именем не зарегистрирована."""


def register_strategy(cls: S) -> S:
    """Декоратор регистрации. Повторное имя — ошибка (две реализации под одним именем)."""
    existing = _REGISTRY.get(cls.name)
    if existing is not None and existing is not cls:
        raise ValueError(f"strategy {cls.name!r} already registered by {existing.__qualname__}")
    _REGISTRY[cls.name] = cls
    return cls


def get_strategy(name: str, params: dict[str, Any] | None = None) -> Strategy:
    """Экземпляр стратегии по имени.

    Raises:
        UnknownStrategyError: имя не зарегистрировано.
        StrategyParamsError: параметры невалидны.
    """
    try:
        cls = _REGISTRY[name]
    except KeyError as e:
        known = ", ".join(sorted(_REGISTRY)) or "-"
        raise UnknownStrategyError(f"unknown strategy {name!r}; known: {known}") from e
    return cls(params)


def list_strategies() -> list[dict[str, Any]]:
    """Описание зарегистрированных стратегий с параметрами по умолчанию, по имени."""
    out: list[dict[str, Any]] = []
    for name in sorted(_REGISTRY):
        strategy = _REGISTRY[name]()
        out.append(
            {
                "name": name,
                "version": strategy.version,
                "params": strategy.params,
                "warmup_bars": strategy.warmup_bars,
            }
        )
    return out
