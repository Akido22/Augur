"""Общие билдеры для тестов Risk Gate: сигнал и контекст «всё в норме» (все чеки проходят)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from augur.adapters.base import InstrumentSpec, MarketStatus
from augur.config.settings import TradingMode
from augur.fusion.contract import RiskFlag
from augur.ids import new_ulid
from augur.risk.models import RiskContext
from augur.signals.ready import SignalReady
from augur.strategies.base import SignalAction

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)

INSTRUMENT = InstrumentSpec(
    symbol="BTC/USDT",
    qty_step=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    min_notional=Decimal("10"),
    price_tick=Decimal("0.01"),
)


def make_signal(
    action: SignalAction = SignalAction.BUY,
    *,
    flags: list[RiskFlag] | None = None,
    stop_loss: Decimal | None = None,
    expires_in: timedelta = timedelta(minutes=30),
    signal_id: str | None = None,
    strategy_signal_ids: list[str] | None = None,
    **over: Any,
) -> SignalReady:
    data: dict[str, Any] = {
        "signal_id": signal_id or new_ulid(),
        "symbol": "BTC/USDT",
        "action": action,
        "strength": 0.8,
        "source": "classic",
        "explanation_ref": None,
        "risk_flags": flags or [],
        "strategy_signal_ids": strategy_signal_ids or [],
        "fused_signal_id": None,
        "created_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + expires_in,
        "stop_loss": stop_loss,
    }
    data.update(over)
    return SignalReady(**data)


def make_ctx(**over: Any) -> RiskContext:
    data: dict[str, Any] = {
        "now": NOW,
        "mode": TradingMode.PAPER,
        "cb_state": "ARMED",
        "market_status": MarketStatus.OPEN,
        "price": Decimal("100"),
        "spread_bps": Decimal("5"),
        "volatility_zscore": Decimal("1"),
        "liquidity_quote": Decimal("1000000"),
        "instrument": INSTRUMENT,
        "equity": Decimal("10000"),
        "cash": Decimal("10000"),
        "margin_used": Decimal("0"),
        "equity_start_of_day": Decimal("10000"),
        "day_pnl": Decimal("0"),
    }
    data.update(over)
    return RiskContext(**data)
