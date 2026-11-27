"""Настройки приложения из окружения и `.env` (ARCHITECTURE.md §2.2).

Секреты хранятся только как `SecretStr` и никогда не попадают в `config_hash`,
логи или `repr`.
"""

import hashlib
import json
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(StrEnum):
    """Режим работы системы (ARCHITECTURE.md §1.4)."""

    ANALYSIS_ONLY = "ANALYSIS_ONLY"
    SIGNAL_ONLY = "SIGNAL_ONLY"
    PAPER = "PAPER"
    MICRO_LIVE = "MICRO_LIVE"
    FULL_AUTO = "FULL_AUTO"


class RiskLimits(BaseModel):
    """Риск-лимиты. Меняются только человеком через конфиг."""

    max_position_pct_equity: Decimal = Decimal("0.02")  # 2% equity на позицию
    max_daily_loss_pct: Decimal = Decimal("0.03")  # 3% дневной убыток → блок
    max_drawdown_pct: Decimal = Decimal("0.10")  # 10% от пика → CB
    max_symbol_concentration_pct: Decimal = Decimal("0.25")
    max_open_positions: int = 5
    max_spread_bps: Decimal = Decimal("30")
    min_liquidity_quote: Decimal = Decimal("100000")  # мин. 24h объём / глубина
    max_consecutive_losses: int = 4
    micro_live_max_notional: Decimal = Decimal("20")  # в валюте счёта
    volatility_halt_zscore: Decimal = Decimal("4.0")
    # --- Risk Gate (0.5.0, CONFIG_VERSION 2; ARCHITECTURE.md §2.8, §7.2) ---
    # FixedFractionalSizer: риск на сделку до стопа, доля equity.
    risk_per_trade_pct: Decimal = Field(default=Decimal("0.005"), gt=0, le=1)
    # R15: флаги по `code`, блокирующие вход (BUY/SELL) во всех режимах.
    blocking_flags: list[str] = Field(default_factory=lambda: ["news_halt"])
    # R15: дополнительные блокирующие `code` по режиму (ARCH §7.2: fusion_contrarian в MICRO_LIVE).
    blocking_flags_by_mode: dict[TradingMode, list[str]] = Field(
        default_factory=lambda: {TradingMode.MICRO_LIVE: ["fusion_contrarian"]}
    )
    # Мягкие флаги по `code`: sized_qty × risk_flag_multiplier, вердикт REDUCED.
    soft_flags: list[str] = Field(default_factory=lambda: ["news_mixed", "high_vol"])
    risk_flag_multiplier: Decimal = Field(default=Decimal("0.5"), gt=0, lt=1)
    # REDUCE: доля |позиции|, закрываемая сигналом REDUCE [ДОПУЩЕНИЕ].
    reduce_fraction: Decimal = Field(default=Decimal("0.5"), gt=0, le=1)


class FusionSettings(BaseModel):
    """Настройки клиента Fusion (контракт — ML_LLM_FUSION_SPEC.md §4.6)."""

    enabled: bool = False
    endpoint: str | None = None  # None → in-process StubFusionClient
    timeout_ms: int = 1500
    max_invalid_streak: int = 5  # для CB-триггера (ARCHITECTURE.md §7.4)
    # CB-триггер по доле невалидных (ARCH §7.4): > invalid_ratio за последние invalid_window.
    invalid_ratio: Decimal = Field(default=Decimal("0.5"), gt=0, le=1)
    invalid_window: int = Field(default=20, ge=1)


class ExecutionSettings(BaseModel):
    """Исполнение (0.6.0, CONFIG_VERSION 3; ARCHITECTURE.md §6.3, §7.3).

    Издержки — общая модель для бэктеста и PaperAdapter (`augur.backtest.costs.CostModel`),
    в процентах: 0.1 = 0.1 %.
    """

    commission_pct: Decimal = Field(default=Decimal("0.1"), ge=0, lt=100)
    slippage_pct: Decimal = Field(default=Decimal("0.05"), ge=0, lt=100)
    # Остаток PARTIAL MARKET-ордера отменяется через partial_timeout_s [ДОПУЩЕНИЕ ARCH §7.3].
    partial_timeout_s: int = Field(default=60, gt=0)
    reconcile_interval_s: int = Field(default=30, gt=0)
    paper_start_cash: Decimal = Field(default=Decimal("10000"), gt=0)


class CircuitBreakerSettings(BaseModel):
    """Circuit breaker (ARCHITECTURE.md §7.4). Пороги убытков — в `RiskLimits`."""

    flatten_on_trip: bool = False  # закрывать позиции при срабатывании (по умолчанию — нет)
    adapter_error_window_s: int = Field(default=300, gt=0)  # 5 мин
    adapter_error_max: int = Field(default=5, ge=1)  # ≥ 5 RetryableError в окне → trip
    reset_code_ttl_s: int = Field(default=120, gt=0)  # одноразовый код для `cb reset`


class ModeSettings(BaseModel):
    """Смена режима и MICRO_LIVE (0.7.0, CONFIG_VERSION 4; ARCHITECTURE.md §1.4, §7.1)."""

    # Одноразовый код для `augur mode set` и `augur orders approve` (6 цифр).
    code_ttl_s: int = Field(default=120, gt=0)
    # FULL_AUTO: минимум исполненных сделок в MICRO_LIVE без срабатывания CB [ДОПУЩЕНИЕ ARCH §1.4].
    min_micro_trades: int = Field(default=20, ge=1)


Importance = Literal["low", "medium", "high"]


class NewsFlag(BaseModel):
    """Флаг, который добавляет реакция `add_risk_flag` (формат `RiskFlag` FUSION SPEC §4.6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    severity: Importance


class NewsRuleWhen(BaseModel):
    """Условия правила (ARCHITECTURE.md §8.4). Все заданные условия должны выполняться."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sentiment_lt: float | None = Field(default=None, ge=-1, le=1)
    sentiment_gt: float | None = Field(default=None, ge=-1, le=1)
    abs_sentiment_lt: float | None = Field(default=None, gt=0, le=1)
    confidence_gte: float | None = Field(default=None, ge=0, le=1)
    importance_in: list[Importance] | None = None
    # Не меньше N новостей по символу за window_min (с учётом текущей), отфильтрованных по
    # importance_in, если оно задано.
    window_count_gte: int | None = Field(default=None, ge=1)
    window_min: int = Field(default=60, gt=0)


class NewsRule(BaseModel):
    """Правило `NewsReactionRules`. Действия только ужесточают риск (ARCHITECTURE.md §8.4).

    `reduce_risk(multiplier)` и `macro_event_blackout` (нужен экономический календарь) в 0.8.0
    не поддерживаются: уменьшение размера — через `add_risk_flag` с кодом из `risk.soft_flags`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    when: NewsRuleWhen = NewsRuleWhen()
    action: Literal["pause_symbol", "pause_strategy", "add_risk_flag", "notify"]
    duration_min: int = Field(default=60, gt=0)
    flag: NewsFlag | None = None  # только для add_risk_flag
    strategies: list[str] = Field(default_factory=list)  # только для pause_strategy

    @model_validator(mode="after")
    def _action_args(self) -> Self:
        if (self.action == "add_risk_flag") != (self.flag is not None):
            raise ValueError("flag is required for add_risk_flag and only for it")
        if (self.action == "pause_strategy") != bool(self.strategies):
            raise ValueError("strategies are required for pause_strategy and only for it")
        return self


def _default_news_rules() -> list[NewsRule]:
    """Стартовые правила ARCHITECTURE.md §8.4 (без macro_event_blackout)."""
    return [
        NewsRule(
            name="strong_negative_pause",
            when=NewsRuleWhen(sentiment_lt=-0.6, confidence_gte=0.6, importance_in=["high"]),
            action="pause_symbol",
            duration_min=60,
        ),
        NewsRule(
            name="mixed_reduce_risk",
            when=NewsRuleWhen(abs_sentiment_lt=0.2, importance_in=["high"], window_count_gte=3),
            action="add_risk_flag",
            flag=NewsFlag(code="news_mixed", severity="medium"),
            duration_min=60,
        ),
        NewsRule(name="any_high_alert", when=NewsRuleWhen(importance_in=["high"]), action="notify"),
    ]


class NewsSettings(BaseModel):
    """Новостной контур (0.8.0, CONFIG_VERSION 5; ARCHITECTURE.md §8). Без LLM."""

    lookback_h: int = Field(default=6, gt=0)  # окно первого fetch и пересечения курсора
    poll_interval_s: int = Field(default=60, gt=0)  # для оркестратора (poll_news, ARCH §2.12)
    timeout_s: float = Field(default=10.0, gt=0)
    finnhub_base_url: str = "https://finnhub.io/api/v1"
    finnhub_categories: list[str] = Field(default_factory=lambda: ["crypto"])
    # Алиасы для привязки к символам (дополнительно к базовому активу: BTC/USDT → BTC).
    symbol_aliases: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "BTC/USDT": ["bitcoin"],
            "ETH/USDT": ["ethereum", "ether"],
        }
    )
    # Вес источника умножает confidence RuleBasedScorer; нет в словаре — 1.0.
    source_weights: dict[str, float] = Field(default_factory=lambda: {"finnhub": 1.0})
    summary_max_chars: int = Field(default=1000, gt=0)
    rules: list[NewsRule] = Field(default_factory=_default_news_rules)


class Settings(BaseSettings):
    """Корневые настройки. Режим по умолчанию — PAPER."""

    model_config = SettingsConfigDict(env_file=".env", env_nested_delimiter="__", extra="forbid")

    trading_mode: TradingMode = TradingMode.PAPER
    max_allowed_mode: TradingMode = TradingMode.PAPER
    db_path: str = "data/augur.db"
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT"])
    risk: RiskLimits = RiskLimits()
    fusion: FusionSettings = FusionSettings()
    execution: ExecutionSettings = ExecutionSettings()
    cb: CircuitBreakerSettings = CircuitBreakerSettings()
    mode: ModeSettings = ModeSettings()
    news: NewsSettings = NewsSettings()
    # Секреты — только SecretStr, только из окружения
    ccxt_exchange: str = "binance"
    ccxt_api_key: SecretStr | None = None
    ccxt_api_secret: SecretStr | None = None
    # true — testnet биржи (ccxt set_sandbox_mode). Реальные деньги только при явном false.
    ccxt_sandbox: bool = True
    ccxt_quote_currency: str = "USDT"  # валюта equity spot-счёта (ARCH §4.4)
    mt5_login: int | None = None
    mt5_password: SecretStr | None = None
    mt5_server: str | None = None
    mt5_server_utc_offset: int = (
        0  # часы: время сервера брокера − UTC (ARCH §4.3, задаётся вручную)
    )
    alpaca_api_key: SecretStr | None = None
    alpaca_api_secret: SecretStr | None = None
    alpaca_paper: bool = True
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    finnhub_api_key: SecretStr | None = None
    newsapi_api_key: SecretStr | None = None
    alphavantage_api_key: SecretStr | None = None
    api_auth_token: SecretStr  # обязателен для GUI/API


def load_settings() -> Settings:
    """Загружает настройки из окружения и `.env`.

    Returns:
        Провалидированный экземпляр `Settings`.

    Raises:
        pydantic.ValidationError: обязательное поле отсутствует или значение невалидно.
    """
    return Settings()  # type: ignore[call-arg]  # обязательные поля приходят из окружения


def _secret_fields() -> set[str]:
    """Имена полей `Settings`, объявленных как `SecretStr`."""
    return {
        name
        for name, field in Settings.model_fields.items()
        if field.annotation is not None and "SecretStr" in str(field.annotation)
    }


def config_hash(settings: Settings) -> str:
    """SHA-256 канонического JSON настроек БЕЗ секретов. Пишется в каждый сигнал/ордер.

    Args:
        settings: загруженные настройки.

    Returns:
        Шестнадцатеричный SHA-256 (64 символа).
    """
    data = settings.model_dump(mode="json", exclude=_secret_fields())
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
