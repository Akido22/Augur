"""DTO Risk Gate (ARCHITECTURE.md §2.8, §7.2).

`RiskContext` — снимок всего, что нужно чекам R1–R18: Risk Gate не ходит ни в БД, ни в сеть,
поэтому вердикт детерминирован по `(SignalReady, RiskContext, RiskLimits)` и воспроизводим
(replay, golden-тесты). Отсутствующие рыночные данные (`None`) → соответствующий чек
не проходит (fail-closed).
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from augur.adapters.base import InstrumentSpec, MarketStatus
from augur.config.settings import TradingMode
from augur.fusion.contract import RiskFlag

__all__ = ["CBState", "CheckResult", "RiskContext", "RiskVerdict", "VerdictKind"]

CBState = Literal["ARMED", "TRIPPED", "KILLED"]
VerdictKind = Literal["APPROVED", "REDUCED", "REJECTED"]


class CheckResult(BaseModel):
    """Результат одного pre-trade чека. Decimal-значения — строкой."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str  # "signal_ttl", "daily_loss", ...
    passed: bool
    value: str | None = None  # фактическое значение
    limit: str | None = None
    reason: str | None = None


class RiskVerdict(BaseModel):
    """Вердикт Risk Gate (ARCHITECTURE.md §2.8).

    `risk_verdict` — значение для `decision_audit.risk_verdict` (ARCH §7.2: REDUCED, если
    объём уменьшен soft-флагом). Поле — расширение схемы §2.8: в `approved: bool` нет различия
    APPROVED/REDUCED, а аудиту оно нужно.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict_id: str
    signal_id: str
    approved: bool
    risk_verdict: VerdictKind
    risk_reason: str | None
    checks: list[CheckResult]
    sized_qty: Decimal | None  # итоговый объём в единицах инструмента; None при отказе
    sized_notional: Decimal | None
    mode: TradingMode
    evaluated_at: datetime

    @property
    def failed(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]


class RiskContext(BaseModel):
    """Вход Risk Gate помимо сигнала. Деньги — `Decimal`, время — aware UTC.

    Источники (ARCHITECTURE.md §7.2): режим — `mode_state`; CB — `cb_state`; позиции —
    `positions` текущего режима; дневной PnL — `portfolio_snapshots`; рынок — адаптер
    (`market_status`, tick, инструмент, `get_account_state`); ордера — `orders`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime
    mode: TradingMode
    cb_state: CBState
    # --- рынок (None = нет данных → чек не проходит) ---
    market_status: MarketStatus | None = None
    price: Decimal | None = Field(default=None, gt=0)  # референсная цена для sizing
    spread_bps: Decimal | None = None
    volatility_zscore: Decimal | None = None
    liquidity_quote: Decimal | None = None  # 24h объём / глубина в валюте котировки
    instrument: InstrumentSpec | None = None
    # --- счёт ---
    equity: Decimal | None = None
    cash: Decimal | None = None
    margin_used: Decimal = Decimal(0)
    equity_start_of_day: Decimal | None = None
    day_pnl: Decimal | None = None
    # --- состояние портфеля и ордеров ---
    positions: dict[str, Decimal] = Field(default_factory=dict)  # symbol → qty (знак = сторона)
    unknown_order_symbols: frozenset[str] = frozenset()  # R5: ордера в UNKNOWN
    active_intents: frozenset[str] = frozenset()  # R17: "<signal_id>|<side>" активных ордеров
    # --- новостной контур (0.8.0; источник — `news_reactions`, ARCH §8.4) ---
    news_paused_symbols: frozenset[str] = frozenset()  # R16
    news_paused_strategies: frozenset[str] = frozenset()  # R16
    # Активные флаги add_risk_flag по символу сигнала: в R15 и soft-sizing вместе с
    # `sig.risk_flags` (только ужесточают).
    news_flags: tuple[RiskFlag, ...] = ()
    signal_strategies: frozenset[str] = frozenset()  # strategy_version родителей сигнала

    @field_validator("now")
    @classmethod
    def _aware_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("now must be timezone-aware (UTC)")
        return v.astimezone(UTC)

    @property
    def open_positions(self) -> dict[str, Decimal]:
        return {s: q for s, q in self.positions.items() if q != 0}
