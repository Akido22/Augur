"""Настройки приложения из окружения и `.env` (ARCHITECTURE.md §2.2).

Секреты хранятся только как `SecretStr` и никогда не попадают в `config_hash`,
логи или `repr`.
"""

import hashlib
import json
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, SecretStr
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


class Settings(BaseSettings):
    """Корневые настройки. Режим по умолчанию — PAPER."""

    model_config = SettingsConfigDict(env_file=".env", env_nested_delimiter="__", extra="forbid")

    trading_mode: TradingMode = TradingMode.PAPER
    max_allowed_mode: TradingMode = TradingMode.PAPER
    db_path: str = "data/augur.db"
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT"])
    risk: RiskLimits = RiskLimits()
    fusion: FusionSettings = FusionSettings()
    # Секреты — только SecretStr, только из окружения
    ccxt_exchange: str = "binance"
    ccxt_api_key: SecretStr | None = None
    ccxt_api_secret: SecretStr | None = None
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
