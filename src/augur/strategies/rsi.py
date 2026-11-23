"""RSI: BUY при RSI < oversold, CLOSE при RSI > overbought."""

from collections.abc import Sequence
from typing import Any, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from augur.data.models import Bar
from augur.strategies.base import Signal, SignalAction, Strategy
from augur.strategies.indicators import rsi
from augur.strategies.registry import register_strategy

# RSI Уайлдера зависит от всей истории; считаем на фиксированном окне
# (LOOKBACK_MULT × period изменений), чтобы решение было детерминированным
# функцией последних `warmup_bars` баров и не зависело от длины выборки.
LOOKBACK_MULT = 4


class RsiParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    period: int = Field(14, ge=2)
    oversold: float = Field(30.0, gt=0, lt=100)
    overbought: float = Field(70.0, gt=0, lt=100)

    @model_validator(mode="after")
    def _levels_order(self) -> Self:
        if self.oversold >= self.overbought:
            raise ValueError("oversold must be < overbought")
        return self


@register_strategy
class RsiStrategy(Strategy):
    name = "rsi"
    version = "rsi_v1"
    params_model = RsiParams

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.p = cast(RsiParams, self.config)

    @property
    def warmup_bars(self) -> int:
        return LOOKBACK_MULT * self.p.period + 1

    def on_bar(self, bar: Bar, history: Sequence[Bar]) -> Signal | None:
        p = self.p
        window = self.warmup_bars
        closes = [b.close for b in history[-(window - 1) :]] + [bar.close]
        if len(closes) < window:
            return None
        value = rsi(closes, p.period)
        if value is None:
            return None
        if value < p.oversold:
            strength = min(1.0, (p.oversold - value) / p.oversold)
            return Signal(
                action=SignalAction.BUY,
                strength=strength,
                reason=f"RSI{p.period}={value:.2f} < {p.oversold:g}",
                timestamp=bar.ts_open,
            )
        if value > p.overbought:
            strength = min(1.0, (value - p.overbought) / (100.0 - p.overbought))
            return Signal(
                action=SignalAction.CLOSE,
                strength=strength,
                reason=f"RSI{p.period}={value:.2f} > {p.overbought:g}",
                timestamp=bar.ts_open,
            )
        return None
