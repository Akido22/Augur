"""Расчёт размера позиции (ARCHITECTURE.md §2.8). Деньги — только `Decimal`.

Sizer возвращает «сырой» объём; ограничения (cap по `max_position_pct_equity`, micro-cap,
soft-флаги, округление вниз до `qty_step`) применяет Risk Gate — и затем проверяет чеком R13.
"""

from decimal import ROUND_DOWN, Decimal
from typing import Protocol

from augur.adapters.base import InstrumentSpec
from augur.config.settings import RiskLimits
from augur.signals.ready import SignalReady
from augur.strategies.base import SignalAction

__all__ = ["FixedFractionalSizer", "PositionSizer", "notional", "round_down_to_step"]


def round_down_to_step(qty: Decimal, step: Decimal) -> Decimal:
    """Округление объёма вниз до шага инструмента (никогда не увеличивает).

    Результат записан с экспонентой шага — детерминированное строковое представление.
    """
    if qty <= 0:
        return Decimal(0)
    if step <= 0:
        return qty
    units = (qty / step).to_integral_value(rounding=ROUND_DOWN)
    return (units * step).quantize(step)  # запись с точностью шага: 2.000 при step 0.001


def notional(qty: Decimal, price: Decimal, instrument: InstrumentSpec) -> Decimal:
    """Стоимость позиции в валюте котировки: qty × price × contract_size."""
    return qty * price * instrument.contract_size


class PositionSizer(Protocol):
    def size(
        self,
        sig: SignalReady,
        equity: Decimal,
        price: Decimal,
        stop: Decimal | None,
        instrument: InstrumentSpec,
    ) -> Decimal: ...


class FixedFractionalSizer:
    """Риск `risk_per_trade_pct` × equity на сделку до стопа (по умолчанию).

    - Есть стоп: qty = equity × r / (|price − stop| × contract_size).
      Стоп не с той стороны (BUY: stop ≥ price; SELL: stop ≤ price) → 0.
    - Стопа нет: qty = equity × max_position_pct_equity / (price × contract_size).
    - Результат ограничен сверху `max_position_pct_equity` × equity по notional.
    - Неоткрывающее действие или equity ≤ 0 → 0.
    """

    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits

    def size(
        self,
        sig: SignalReady,
        equity: Decimal,
        price: Decimal,
        stop: Decimal | None,
        instrument: InstrumentSpec,
    ) -> Decimal:
        if sig.action not in (SignalAction.BUY, SignalAction.SELL) or equity <= 0 or price <= 0:
            return Decimal(0)
        unit_value = price * instrument.contract_size
        cap_qty = equity * self._limits.max_position_pct_equity / unit_value
        if stop is None:
            return cap_qty
        distance = price - stop if sig.action == SignalAction.BUY else stop - price
        if distance <= 0:
            return Decimal(0)
        risk_qty = equity * self._limits.risk_per_trade_pct / (distance * instrument.contract_size)
        return min(risk_qty, cap_qty)
