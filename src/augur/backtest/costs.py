"""Модель издержек, общая для бэктеста и PaperAdapter (ARCHITECTURE.md §4.6, §6.3).

- Проскальзывание: покупка по `ref × (1 + s)`, продажа по `ref × (1 − s)`.
- Комиссия: `notional × c`.
`s`, `c` задаются в процентах (0.1 = 0.1 %). Decimal-методы — для исполнения (PaperAdapter),
float-методы — для бэктеста (аналитика на `float`, как и `ohlcv`).

Модуль без зависимостей от остального пакета: его импортируют и `backtest`, и `adapters`.
"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["CostModel", "OrderSide"]

OrderSide = Literal["BUY", "SELL"]
_HUNDRED = Decimal(100)


class CostModel(BaseModel):
    """Комиссия и проскальзывание в процентах."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commission_pct: Decimal = Field(default=Decimal(0), ge=0, lt=100)
    slippage_pct: Decimal = Field(default=Decimal(0), ge=0, lt=100)

    @classmethod
    def from_floats(cls, commission_pct: float, slippage_pct: float) -> "CostModel":
        return cls(
            commission_pct=Decimal(str(commission_pct)), slippage_pct=Decimal(str(slippage_pct))
        )

    # --- Decimal (исполнение) -------------------------------------------------------

    def fill_price(self, side: OrderSide, ref: Decimal) -> Decimal:
        """Цена исполнения с проскальзыванием против стороны ордера."""
        s = self.slippage_pct / _HUNDRED
        return ref * (1 + s) if side == "BUY" else ref * (1 - s)

    def fee(self, notional: Decimal) -> Decimal:
        """Комиссия от модуля notional."""
        return abs(notional) * self.commission_pct / _HUNDRED

    # --- float (бэктест) ------------------------------------------------------------

    @property
    def commission_rate(self) -> float:
        return float(self.commission_pct) / 100.0

    @property
    def slippage_rate(self) -> float:
        return float(self.slippage_pct) / 100.0

    def fill_price_f(self, side: OrderSide, ref: float) -> float:
        s = self.slippage_rate
        return ref * (1 + s) if side == "BUY" else ref * (1 - s)

    def fee_f(self, notional: float) -> float:
        return abs(notional) * self.commission_rate
