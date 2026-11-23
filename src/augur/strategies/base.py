"""Базовый класс стратегии и сигнал (ARCHITECTURE.md §2.5, §6.1).

Этап 0.3.0 — упрощённый интерфейс: `on_bar(bar, history) -> Signal | None`.
`StrategySignal` из §2.5 (signal_id, params_hash, data_hash) и `StrategyContext`
появятся вместе с Signal Engine (0.4.0).

Стратегия — чистая функция от входа: никакой сети, системного времени и `random`
без seed. Она видит только закрытый текущий бар и бары до него.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from augur.data.models import Bar


class SignalAction(StrEnum):
    """Действие сигнала. NO_SIGNAL ≠ HOLD (ARCHITECTURE.md §2.5)."""

    BUY = "BUY"
    SELL = "SELL"
    CLOSE = "CLOSE"
    REDUCE = "REDUCE"
    HOLD = "HOLD"  # решение принято: ничего не делать
    NO_SIGNAL = "NO_SIGNAL"  # решения нет: входы недостаточны/невалидны


class Signal(BaseModel):
    """Сигнал стратегии. `timestamp` — `ts_open` закрытого бара решения, Unix ms UTC."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: SignalAction
    strength: float = Field(ge=0.0, le=1.0)
    reason: str
    timestamp: int = Field(ge=0)


class StrategyParamsError(ValueError):
    """Параметры стратегии не прошли валидацию `params_model`."""


class Strategy(ABC):
    """Базовый класс стратегии.

    Подкласс задаёт `name`, `version` (`<имя>_v<N>`, VERSIONING.md §2.3), `params_model`
    (Pydantic-модель параметров с дефолтами) и реализует `warmup_bars` и `on_bar`.
    """

    name: ClassVar[str]
    version: ClassVar[str]
    params_model: ClassVar[type[BaseModel]]

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        """
        Args:
            params: переопределения параметров; недостающие берутся из дефолтов `params_model`.

        Raises:
            StrategyParamsError: неизвестный ключ или недопустимое значение.
        """
        try:
            self.config: BaseModel = self.params_model.model_validate(params or {})
        except ValidationError as e:
            raise StrategyParamsError(f"{self.name}: invalid params: {e}") from e

    @property
    def params(self) -> dict[str, Any]:
        """Итоговые параметры (дефолты + переопределения)."""
        return self.config.model_dump()

    @property
    @abstractmethod
    def warmup_bars(self) -> int:
        """Сколько баров (включая текущий) нужно, чтобы стратегия могла принять решение."""

    @abstractmethod
    def on_bar(self, bar: Bar, history: Sequence[Bar]) -> Signal | None:
        """Решение на закрытом баре.

        Args:
            bar: текущий закрытый бар.
            history: бары строго до `bar` (по возрастанию `ts_open`), без будущего.

        Returns:
            Сигнал или None, если действия нет.
        """
