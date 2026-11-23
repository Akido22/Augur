"""SMA Crossover: BUY при пересечении SMA(short) снизу вверх SMA(long), CLOSE — сверху вниз."""

from collections.abc import Sequence
from typing import Any, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from augur.data.models import Bar
from augur.strategies.base import Signal, SignalAction, Strategy
from augur.strategies.indicators import sma
from augur.strategies.registry import register_strategy


class SmaCrossParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    short: int = Field(10, ge=1)
    long: int = Field(30, ge=2)

    @model_validator(mode="after")
    def _short_lt_long(self) -> Self:
        if self.short >= self.long:
            raise ValueError("short must be < long")
        return self


@register_strategy
class SmaCross(Strategy):
    name = "sma_cross"
    version = "sma_cross_v1"
    params_model = SmaCrossParams

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.p = cast(SmaCrossParams, self.config)

    @property
    def warmup_bars(self) -> int:
        # SMA(long) на текущем и предыдущем баре — для детекции пересечения.
        return self.p.long + 1

    def on_bar(self, bar: Bar, history: Sequence[Bar]) -> Signal | None:
        p = self.p
        closes = [b.close for b in history[-p.long :]] + [bar.close]
        if len(closes) < p.long + 1:
            return None
        prev, curr = closes[:-1], closes[1:]
        s_prev, l_prev = sma(prev, p.short), sma(prev, p.long)
        s_curr, l_curr = sma(curr, p.short), sma(curr, p.long)
        if s_prev is None or l_prev is None or s_curr is None or l_curr is None:
            return None
        if s_prev <= l_prev and s_curr > l_curr:
            action = SignalAction.BUY
            reason = f"SMA{p.short} crossed above SMA{p.long}: {s_curr:.6g} > {l_curr:.6g}"
        elif s_prev >= l_prev and s_curr < l_curr:
            action = SignalAction.CLOSE
            reason = f"SMA{p.short} crossed below SMA{p.long}: {s_curr:.6g} < {l_curr:.6g}"
        else:
            return None
        return Signal(action=action, strength=1.0, reason=reason, timestamp=bar.ts_open)
