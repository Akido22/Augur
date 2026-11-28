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


class FusionWeights(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ml: float = Field(default=0.5, ge=0, le=1)
    tech: float = Field(default=0.3, ge=0, le=1)
    llm: float = Field(default=0.2, ge=0, le=1)

    @model_validator(mode="after")
    def _sum_is_one(self) -> Self:
        if abs(self.ml + self.tech + self.llm - 1.0) > 1e-9:
            raise ValueError("fusion weights must sum to 1")
        return self


class FusionConflict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    llm_opposite_threshold: float = Field(default=0.5, gt=0, le=1)
    strength_multiplier: float = Field(default=0.5, ge=0, le=1)


class FusionConfig(BaseModel):
    """Веса, пороги и veto `fuse()` (ML_LLM_FUSION_SPEC.md §4.2). Числа [Д8] — стартовые.

    Любое изменение значений — новая `config_version` (§4.2): по ней воспроизводится решение.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_version: str = Field(default="fusion-2026.11.1", min_length=1)
    weights: FusionWeights = FusionWeights()
    ml_min_confidence: float = Field(default=0.55, gt=0, lt=1)
    entry_threshold: float = Field(default=0.30, gt=0, le=1)
    strong_threshold: float = Field(default=0.60, gt=0, le=1)
    llm_can_amplify: bool = False
    llm_max_age_s: int = Field(default=1800, gt=0)
    blocked_entry_regimes: list[str] = Field(default_factory=lambda: ["chaotic"])
    conflict: FusionConflict = FusionConflict()
    medium_flag_multiplier: float = Field(default=0.5, ge=0, le=1)
    on_high_flag_with_open_position: Literal["reduce", "hold", "exit"] = "reduce"
    ml_only_strength_multiplier: float = Field(default=0.5, ge=0, le=1)
    signal_ttl_fraction_of_bar: float = Field(default=0.5, gt=0, le=1)


class MLFeatureSettings(BaseModel):
    """Признаки (FUSION SPEC §2.2). Изменение любого поля меняет `feature_set_version`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    return_lags: list[int] = Field(default_factory=lambda: [1, 2, 4, 8, 16, 32])
    window: int = Field(default=200, ge=120)  # хвост истории на один вектор = warm-up


class MLRegimeSettings(BaseModel):
    """Пороги детерминированного `regime_tag` (skill ml-training-inference, п. 12)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chaotic_rv_ratio: float = Field(default=2.5, gt=0)  # RV(20)/RV(100) выше и нет тренда
    high_vol_atr_pct: float = Field(default=0.9, gt=0, le=1)  # перцентиль ATR в окне
    trend_adx: float = Field(default=25.0, gt=0, le=100)


class MLTrainSettings(BaseModel):
    """Разметка, walk-forward и гиперпараметры (FUSION SPEC §2.1, §2.3). Фиксированы: trials=1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    barrier_k: float = Field(default=1.5, gt=0)
    horizon_bars: int = Field(default=4, ge=1)
    train_bars: int = Field(default=2000, ge=100)
    test_bars: int = Field(default=500, ge=20)
    calib_fraction: float = Field(default=0.15, gt=0, lt=1)
    calib_min: int = Field(default=200, ge=20)
    isotonic_min: int = Field(default=1000, ge=10)  # иначе Platt
    expanding: bool = False
    seed: int = 42
    num_leaves: int = Field(default=15, ge=2)
    learning_rate: float = Field(default=0.05, gt=0)
    n_estimators: int = Field(default=200, ge=1)
    min_data_in_leaf: int = Field(default=50, ge=1)


class MLPromotionSettings(BaseModel):
    """Критерии промоушена в production (FUSION SPEC §2.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_ece: float = Field(default=0.05, gt=0, le=1)
    min_staging_days: int = Field(default=14, ge=0)


class MLSettings(BaseModel):
    """ML-слой `augur.fusion.ml` (FUSION SPEC §2, skill ml-training-inference)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifacts_dir: str = "artifacts/ml"
    venue: str = "ccxt:binance"  # venue баров в `ohlcv` для признаков
    features: MLFeatureSettings = MLFeatureSettings()
    regime: MLRegimeSettings = MLRegimeSettings()
    train: MLTrainSettings = MLTrainSettings()
    promotion: MLPromotionSettings = MLPromotionSettings()


class LLMPrice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input: float = Field(default=0.0, ge=0)  # USD за 1M токенов [Д6]
    output: float = Field(default=0.0, ge=0)


class LLMProvider(BaseModel):
    """OpenAI-совместимый провайдер (FUSION SPEC §3.4). Ключ — поле `Settings.llm_<key>_key`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key: Literal["anthropic", "deepseek", "openai"] | None = None
    price_per_mtok: LLMPrice = LLMPrice()
    # false — `temperature` не отправляется (часть моделей, напр. Claude Sonnet 5.5, отвечает 400
    # на нестандартные значения); разнообразие сэмплов даёт перестановка новостей (§5.2).
    send_temperature: bool = False


class LLMVarianceSettings(BaseModel):
    """Variance filter (FUSION SPEC §5.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    samples: int = Field(default=3, ge=1)
    min_valid: int = Field(default=2, ge=1)
    temperature: float = Field(default=0.5, ge=0, le=2)
    max_score_std: float = Field(default=0.25, ge=0)
    max_score_range: float = Field(default=0.6, ge=0)


class LLMSettings(BaseModel):
    """LLM-слой `augur.fusion.llm` (FUSION SPEC §3). По умолчанию выключен: Fusion = ML-only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["enabled", "ml_only", "disabled"] = "disabled"
    daily_usd: float = Field(default=10.0, ge=0)
    event_only_budget_fraction: float = Field(default=0.8, gt=0, le=1)  # ≥ 80 % — только события
    max_calls_per_asset_hour: int = Field(default=6, ge=1)
    timeout_s: float = Field(default=8.0, gt=0)
    max_retries: int = Field(default=1, ge=0, le=1)  # повтор с текстом ошибки, не «починка»
    max_tokens: int = Field(default=800, ge=64)
    news_window_h: int = Field(default=6, gt=0)
    max_news: int = Field(default=20, ge=1)
    news_body_chars: int = Field(default=500, ge=50)
    empty_input_max_abs_score: float = Field(default=0.2, ge=0, le=1)
    variance: LLMVarianceSettings = LLMVarianceSettings()
    # LLM-breaker (§5.4): доля отклонённых за час > порога при ≥ min_calls → ML-only.
    breaker_reject_ratio: float = Field(default=0.2, gt=0, le=1)
    breaker_min_calls: int = Field(default=10, ge=1)
    providers: list[LLMProvider] = Field(
        default_factory=lambda: [
            LLMProvider(
                name="anthropic",
                base_url="https://api.anthropic.com/v1/",
                model="claude-sonnet-5-5",
                api_key="anthropic",
            )
        ]
    )


class FusionSettings(BaseModel):
    """Настройки Fusion (контракт — ML_LLM_FUSION_SPEC.md §4.6).

    `enabled=false` → только классика. `enabled=true` без `endpoint` → in-process
    `LocalFusionClient` (0.10.0): ML + LLM-контекст + `fuse()`.
    """

    enabled: bool = False
    endpoint: str | None = None  # None → in-process LocalFusionClient (0.10.0)
    config: FusionConfig = FusionConfig()
    ml: MLSettings = MLSettings()
    llm: LLMSettings = LLMSettings()
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
    # 0.10.0: "llm" — LLM-скорер Fusion (`fusion:prompt_news_v1`) с fallback на rule:v1.
    scorer: Literal["rule", "llm"] = "rule"
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
    # Ключи LLM (FUSION SPEC §3.6): передаются только LLM-клиенту, не Execution.
    llm_anthropic_key: SecretStr | None = None
    llm_deepseek_key: SecretStr | None = None
    llm_openai_key: SecretStr | None = None
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
