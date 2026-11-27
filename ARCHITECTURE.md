# ARCHITECTURE.md — спецификация торговой платформы

> **Версия:** 1.0 · **Дата:** 2026-11-20 · **Статус:** Draft for implementation
> **Связанный документ:** `ML_LLM_FUSION_SPEC.md` — ML-модели, LLM-провайдеры, правила слияния, защита от галлюцинаций. В этом документе гибридный контур описан **только как интерфейсный контракт**.

**Легенда пометок:**
- `[ДОПУЩЕНИЕ]` — решение принято автором спецификации, его нужно подтвердить.
- `[→ FUSION SPEC]` — деталь находится в `ML_LLM_FUSION_SPEC.md`, здесь не раскрывается.
- `MUST / MUST NOT / SHOULD` — нормативные требования (RFC 2119).

---

## Оглавление

1. [Обзор системы](#1-обзор-системы)
2. [Архитектура и модули](#2-архитектура-и-модули)
3. [Модель данных и SQLite](#3-модель-данных-и-sqlite)
4. [Интеграция с рынками](#4-интеграция-с-рынками)
5. [Интерфейс с гибридным ML+LLM контуром (контракт)](#5-интерфейс-с-гибридным-mlllm-контуром-контракт-без-деталей)
6. [Стратегии и бэктестинг](#6-стратегии-и-бэктестинг)
7. [Автоторговля (Execution Layer)](#7-автоторговля-execution-layer)
8. [Новостной контур и реакция](#8-новостной-контур-и-реакция)
9. [GUI и CLI](#9-gui-и-cli)
10. [Безопасность и надёжность](#10-безопасность-и-надёжность)
11. [Структура проекта](#11-структура-проекта)
12. [Требования к агентной разработке](#12-требования-к-агентной-разработке-claude-code--codex)
13. [Skills для Claude Code / Codex](#13-skills-для-claude-code--codex)
14. [План реализации](#14-план-реализации)
15. [Риски и открытые вопросы](#15-риски-и-открытые-вопросы)

---

## 1. Обзор системы

### 1.1. Назначение

Платформа для анализа рынков, генерации сигналов, бэктестинга и автоматической торговли на трёх классах активов (Форекс, крипта, акции) с единым ядром стратегий и жёстким риск-контуром. Ключевые свойства:

| Свойство | Как обеспечивается |
|---|---|
| Модульность рынков | Единый протокол `MarketAdapter`; стратегии не знают о брокере |
| Безопасность | Секреты только из окружения; ключи биржи видит только Execution; Risk Gate нельзя обойти |
| Детерминизм | Все решения детерминированы по входу; хеши данных/конфига/параметров пишутся в аудит; `Decimal` для денег |
| Наблюдаемость | `audit_log` + `decision_audit` — полная цепочка «данные → сигнал → fusion → risk → order» |
| Безопасная эскалация | Режимы PAPER → MICRO_LIVE → FULL_AUTO с явным подтверждением |

### 1.2. Ключевые сценарии

| # | Сценарий | Режим | Участвующие модули |
|---|---|---|---|
| S1 | Анализ рынка (графики, индикаторы, новости) | ANALYSIS_ONLY | Data, News, Interfaces |
| S2 | Генерация сигналов и алерты без исполнения | SIGNAL_ONLY | + Strategies, Signal Engine, Fusion (контракт), Notification |
| S3 | Бэктест стратегии на истории | любой (офлайн) | Data, Strategies, Backtest |
| S4 | Paper-trading в реальном времени | PAPER | + Risk Gate, Execution (PaperBroker) |
| S5 | Live-торговля минимальным объёмом с ручным подтверждением первого ордера | MICRO_LIVE | + реальный адаптер |
| S6 | Полная автоторговля в пределах риск-лимитов | FULL_AUTO | все модули |
| S7 | Аварийная остановка | любой | Kill switch, Circuit Breaker |

### 1.3. Диаграмма компонентов верхнего уровня

```
                ┌──────────────────────────────────────────────────────────────┐
                │                    Orchestrator / Scheduler                  │
                │            (asyncio event bus + periodic jobs)               │
                └──────────────────────────────────────────────────────────────┘
                         │ events                                   ▲ events
┌──────────────┐   ┌─────▼───────────┐   ┌──────────────────┐   ┌──┴───────────┐   ┌──────────────┐
│  DATA LAYER  │──▶│  SIGNAL ENGINE  │──▶│    RISK GATE     │──▶│  EXECUTION   │──▶│  INTERFACES  │
│ OHLCV, ticks │   │ strategies ──┐  │   │ pre-trade checks │   │ OrderManager │   │ FastAPI + WS │
│ news, macro  │   │              ▼  │   │ sizing, TTL,     │   │ idempotency  │   │ CLI (Typer)  │
│ SQLite cache │   │ FusionClient ◀──┼──▶│ market state     │   │ CircuitBreak │   │ Notification │
└──────┬───────┘   │ (contract only) │   └──────────────────┘   │ KillSwitch   │   └──────────────┘
       │           └────────┬────────┘                          └──────┬───────┘
       │                    │ HTTP/in-proc                             │
       │           ┌────────▼────────┐                          ┌──────▼───────┐
       │           │ FUSION MODULE   │  [→ FUSION SPEC]         │   ADAPTERS   │
       │           │ ML + LLM        │                          │ MT5 · CCXT · │
       │           └─────────────────┘                          │ Alpaca·Paper │
       │                                                        └──────────────┘
       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ SQLite (WAL): ohlcv · news · signals · fused_signals · orders · positions ·  │
│ portfolio_snapshots · decision_audit · audit_log · mode_state · cb_state     │
└──────────────────────────────────────────────────────────────────────────────┘
```

Поток событий: `bar_closed` → `strategy_signal` → (`fusion_request` → `fused_signal`) → `signal_ready` → `risk_verdict` → `order_intent` → `order_update` → `position_update`.

### 1.4. Режимы работы

| Режим | Генерация сигналов | Risk Gate | Ордера | Реальные деньги |
|---|---|---|---|---|
| `ANALYSIS_ONLY` | нет | нет | нет | нет |
| `SIGNAL_ONLY` | да | оценивает (информативно) | нет | нет |
| `PAPER` | да | да | симуляция (`PaperBroker`) | нет |
| `MICRO_LIVE` | да | да + micro-лимиты | реальные, мин. размер; 1-й ордер — ручное подтверждение | да (минимально) |
| `FULL_AUTO` | да | да | реальные, автоматически | да |

**Правило переключения (MUST):** режим меняется только при одновременном выполнении трёх условий:
1. Значение `TRADING_MODE` в конфиге равно целевому режиму (или выше — см. `max_allowed_mode`).
2. CLI-команда `augur mode set <MODE> --confirm` с вводом одноразового кода подтверждения (для `MICRO_LIVE`/`FULL_AUTO`).
3. Запись `MODE_CHANGED` в `audit_log` и в `mode_state` прошла успешно (иначе откат).

Понижение режима (например, `FULL_AUTO → PAPER`) разрешено без кода подтверждения. GUI может **инициировать** запрос на смену режима, но подтверждение выполняется через CLI `[ДОПУЩЕНИЕ]` — снижает риск случайного клика и CSRF.

Допустимые переходы вверх: `ANALYSIS_ONLY → SIGNAL_ONLY → PAPER → MICRO_LIVE → FULL_AUTO` — только на один шаг за раз. Переход в `FULL_AUTO` требует, чтобы в `MICRO_LIVE` было исполнено не менее `promotion.min_micro_trades` (по умолчанию 20) сделок без срабатывания circuit breaker `[ДОПУЩЕНИЕ]`.

---

## 2. Архитектура и модули

### 2.0. Общие принципы

- Python 3.11+, `asyncio` везде, где есть I/O. Блокирующие SDK (MetaTrader5) — через `asyncio.to_thread` в выделенном single-thread executor.
- Все денежные величины — `decimal.Decimal`; `float` допустим только в индикаторах/аналитике.
- Все времена — `datetime` с `tzinfo=UTC`; в БД — `INTEGER` миллисекунд Unix UTC.
- Межмодульное взаимодействие — через **типизированные события** (Pydantic v2) на внутренней шине или через публичные `Protocol`-интерфейсы. Прямой импорт внутренностей другого модуля запрещён (проверяется `import-linter`).
- Каждый модуль экспортирует публичный API через `__init__.py`; всё остальное — `_private`.

### 2.1. Карта модулей

| Модуль | Пакет | Ответственность | Зависит от |
|---|---|---|---|
| Config | `src/augur/config` | Загрузка и валидация настроек, секреты, хеш конфига | — |
| Data Layer | `src/augur/data` | OHLCV, тики, новости, макро; нормализация; кэш в SQLite | config, db, adapters (read-only) |
| Market Adapters | `src/augur/adapters` | Унифицированный доступ к MT5 / CCXT / Alpaca / Paper | config |
| Strategy Framework | `src/augur/strategies` | Классические стратегии, реестр | data (DTO) |
| Backtest Engine | `src/augur/backtest` | Событийный бэктест, комиссии, проскальзывание, метрики | strategies, data, risk |
| Signal Engine | `src/augur/signals` | Сбор сигналов стратегий, вызов FusionClient, публикация `signal_ready` | strategies, fusion (контракт) |
| Fusion (контракт) | `src/augur/fusion` | DTO `FusedSignal`, `FusionClient`, заглушка | — |
| Risk Engine | `src/augur/risk` | Pre/post-trade лимиты, sizing, Risk Gate | config, db |
| Execution | `src/augur/execution` | Ордера, позиции, идемпотентность, CB, kill switch, режимы | adapters, risk, db |
| News & Sentiment | `src/augur/news` | Получение новостей, дедуп, скоринг, `news_event` | config, db |
| Notification | `src/augur/notify` | Telegram / Email / Webhook | config |
| Orchestrator | `src/augur/orchestrator` | Event bus, планировщик, жизненный цикл | все |
| Interfaces | `src/augur/interfaces` | FastAPI + WebSocket, CLI (Typer), общие DTO | orchestrator |
| DB | `src/augur/db` | Схема, миграции, репозитории, single-writer | — |

### 2.2. Config / Settings

```python
# src/augur/config/settings.py
from enum import StrEnum
from decimal import Decimal
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class TradingMode(StrEnum):
    ANALYSIS_ONLY = "ANALYSIS_ONLY"
    SIGNAL_ONLY = "SIGNAL_ONLY"
    PAPER = "PAPER"
    MICRO_LIVE = "MICRO_LIVE"
    FULL_AUTO = "FULL_AUTO"

class RiskLimits(BaseModel):
    max_position_pct_equity: Decimal = Decimal("0.02")     # 2% equity на позицию
    max_daily_loss_pct: Decimal = Decimal("0.03")          # 3% дневной убыток → блок
    max_drawdown_pct: Decimal = Decimal("0.10")            # 10% от пика → CB
    max_symbol_concentration_pct: Decimal = Decimal("0.25")
    max_open_positions: int = 5
    max_spread_bps: Decimal = Decimal("30")
    min_liquidity_quote: Decimal = Decimal("100000")       # мин. 24h объём / глубина
    max_consecutive_losses: int = 4
    micro_live_max_notional: Decimal = Decimal("20")       # в валюте счёта
    volatility_halt_zscore: Decimal = Decimal("4.0")

class FusionSettings(BaseModel):
    enabled: bool = False
    endpoint: str | None = None        # None → in-process StubFusionClient
    timeout_ms: int = 1500
    max_invalid_streak: int = 5        # для CB-триггера (п. 7.4)

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_nested_delimiter="__",
                                      extra="forbid")
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
    alpaca_api_key: SecretStr | None = None
    alpaca_api_secret: SecretStr | None = None
    alpaca_paper: bool = True
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    finnhub_api_key: SecretStr | None = None
    newsapi_api_key: SecretStr | None = None
    alphavantage_api_key: SecretStr | None = None
    api_auth_token: SecretStr            # обязателен для GUI/API

def load_settings() -> Settings: ...
def config_hash(settings: Settings) -> str:
    """SHA-256 канонического JSON настроек БЕЗ секретов. Пишется в каждый сигнал/ордер."""
```

Публичный интерфейс: `load_settings()`, `config_hash()`, `Settings`, `TradingMode`, `RiskLimits`.
Секреты не логируются (`SecretStr`), `repr` маскирован; `config validate` проверяет, что для выбранного режима есть нужные ключи.

### 2.3. Data Layer

Ответственность: получение, нормализация, хранение и выдача рыночных данных. Единственный источник данных для стратегий, бэктеста и Fusion-входа.

```python
# src/augur/data/models.py
class Bar(BaseModel, frozen=True):
    symbol: str            # каноническое имя: "BTC/USDT", "EURUSD", "AAPL"
    venue: str             # "ccxt:binance" | "mt5" | "alpaca"
    timeframe: str         # "1m","5m","15m","1h","4h","1d"
    ts_open: int           # Unix ms UTC, начало бара
    open: float; high: float; low: float; close: float; volume: float
    is_closed: bool = True

class Tick(BaseModel, frozen=True):
    symbol: str; venue: str; ts: int   # Unix ms UTC
    bid: Decimal; ask: Decimal; last: Decimal | None = None; volume: float | None = None

# src/augur/data/service.py
class MarketDataService:
    async def get_bars(self, symbol: str, timeframe: str, start: datetime,
                       end: datetime, *, venue: str | None = None) -> list[Bar]:
        """Сначала SQLite-кэш, недостающие диапазоны догружаются через адаптер.
        Возвращает только закрытые бары, отсортированные по ts_open."""
    async def get_latest_tick(self, symbol: str) -> Tick: ...
    async def backfill(self, symbol: str, timeframe: str, days: int) -> int: ...
    def window_hash(self, bars: Sequence[Bar]) -> str:
        """SHA-256 окна входных данных — для воспроизводимости сигнала."""

class SymbolNormalizer:
    def to_canonical(self, venue: str, raw: str) -> str: ...
    def to_venue(self, venue: str, canonical: str) -> str: ...
```

Правила:
- Нормализация: OHLCV проверяется на `high >= max(open, close)`, `low <= min(open, close)`, `volume >= 0`, отсутствие дубликатов; некорректные бары отбрасываются с записью в `audit_log` (`DATA_ANOMALY`).
- Незакрытый (текущий) бар никогда не передаётся стратегиям в режиме `on_bar` (защита от look-ahead в live).
- Макро-данные (`[ДОПУЩЕНИЕ]` — календарь событий из Finnhub/Alpha Vantage) хранятся в таблице `macro_events` (опционально, этап 10).

### 2.4. Market Adapters

См. раздел 4 — полный протокол и маппинг на SDK.

### 2.5. Strategy Framework

```python
# src/augur/strategies/base.py
class SignalAction(StrEnum):
    BUY = "BUY"; SELL = "SELL"; CLOSE = "CLOSE"; REDUCE = "REDUCE"
    HOLD = "HOLD"            # решение принято: ничего не делать
    NO_SIGNAL = "NO_SIGNAL"  # решения нет: входы недостаточны/невалидны (NO_SIGNAL ≠ HOLD)

class StrategySignal(BaseModel, frozen=True):
    signal_id: str              # ULID
    strategy: str               # name@version, напр. "sma_cross@1.2.0"
    params_hash: str
    symbol: str
    timeframe: str
    action: SignalAction
    strength: float             # [0, 1]
    ts: datetime                # = ts_open закрытого бара, на котором принято решение
    data_hash: str              # MarketDataService.window_hash
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    meta: dict[str, float | str] = {}

class StrategyContext(Protocol):
    def history(self, symbol: str, timeframe: str, n: int) -> Sequence[Bar]: ...
    def position(self, symbol: str) -> PositionView | None: ...
    def now(self) -> datetime: ...            # время симуляции в бэктесте, UTC в live

class Strategy(ABC):
    name: ClassVar[str]
    version: ClassVar[str]
    params_model: ClassVar[type[BaseModel]]
    warmup_bars: ClassVar[int]

    def __init__(self, params: BaseModel) -> None: ...
    def on_bar(self, bar: Bar, ctx: StrategyContext) -> None: ...
    def on_tick(self, tick: Tick, ctx: StrategyContext) -> None: ...   # опционально
    @abstractmethod
    def generate_signals(self, ctx: StrategyContext) -> list[StrategySignal]: ...

class StrategyRegistry:
    def register(self, cls: type[Strategy]) -> None: ...
    def create(self, name: str, params: dict) -> Strategy: ...
    def list(self) -> list[StrategyInfo]: ...
```

Требования: стратегия — **чистая функция от контекста** (никаких сетевых вызовов, `random` без seed, системного времени). Одна и та же реализация работает и в бэктесте, и в live.

### 2.6. Backtest Engine

См. раздел 6.

### 2.7. Signal Engine

Точка входа для сигналов. **MUST NOT** содержать ML-кода и вызывать LLM.

```python
# src/augur/signals/engine.py
class SignalReady(BaseModel, frozen=True):
    """Событие signal_ready — единственный вход Risk Gate."""
    signal_id: str                       # ULID нового итогового сигнала
    symbol: str
    action: SignalAction
    strength: float                      # [0, 1]
    source: Literal["classic", "fusion", "classic_fallback"]
    explanation_ref: str | None          # ссылка на объяснение (fused_signals.id / llm ref) [→ FUSION SPEC]
    risk_flags: list[str]                # напр. ["news_negative", "high_vol", "ml_disabled"]
    strategy_signal_ids: list[str]
    fused_signal_id: str | None
    created_at: datetime
    expires_at: datetime                 # created_at + ttl_seconds
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None

class SignalEngine:
    def __init__(self, registry: StrategyRegistry, fusion: FusionClient,
                 bus: EventBus, repo: SignalRepository, settings: Settings) -> None: ...
    async def on_bar_closed(self, bar: Bar) -> list[SignalReady]:
        """1) прогнать стратегии; 2) сохранить StrategySignal;
        3) если fusion.enabled — FusionClient.fuse(); валидировать;
        4) при ошибке/таймауте/невалидности — fallback на классику (ML-disabled) + warning в audit_log;
        5) сохранить и опубликовать signal_ready."""
    def combine_classic(self, signals: list[StrategySignal]) -> SignalReady | None:
        """Детерминированная агрегация классических сигналов (без ML):
        конфликт BUY/SELL → HOLD; strength = max по направлению большинства."""
```

**`SignalAction` (6 значений):** `BUY`, `SELL`, `CLOSE`, `REDUCE`, `HOLD`, `NO_SIGNAL`.

- `HOLD` — источник оценил ситуацию и решил не действовать (порог не пройден, veto, конфликт).
- `NO_SIGNAL` — оценки нет: обязательный вход отсутствует, устарел или невалиден. `NO_SIGNAL` ≠ `HOLD`; в аудите и аналитике считаются раздельно.
- Оба значения неисполняемы: Risk Gate отклоняет их чеком R4.

**Маппинг Fusion → Signal Engine** (`action` из `ML_LLM_FUSION_SPEC.md` §4.5–4.6 → `SignalAction`; выполняется на границе `FusionClient` до структурной валидации п. 5.5):

| Fusion `action` | `SignalAction` |
|---|---|
| `enter_long` | `BUY` |
| `enter_short` | `SELL` |
| `exit` | `CLOSE` |
| `reduce` | `REDUCE` |
| `hold` | `HOLD` |
| `no_signal` | `NO_SIGNAL` |

### 2.8. Risk Engine

```python
# src/augur/risk/gate.py
class CheckResult(BaseModel, frozen=True):
    name: str                 # "signal_ttl", "daily_loss", ...
    passed: bool
    value: str | None = None  # фактическое значение (str для Decimal)
    limit: str | None = None
    reason: str | None = None

class RiskVerdict(BaseModel, frozen=True):
    verdict_id: str
    signal_id: str
    approved: bool
    checks: list[CheckResult]
    sized_qty: Decimal | None          # итоговый объём в единицах инструмента
    sized_notional: Decimal | None
    mode: TradingMode
    evaluated_at: datetime

class RiskGate:
    async def evaluate(self, sig: SignalReady, ctx: RiskContext) -> RiskVerdict: ...

class PositionSizer(Protocol):
    def size(self, sig: SignalReady, equity: Decimal, price: Decimal,
             stop: Decimal | None, instrument: InstrumentSpec) -> Decimal: ...

class FixedFractionalSizer:  # по умолчанию: риск r% equity на сделку до стопа
    ...

class PostTradeMonitor:
    async def on_fill(self, fill: FillEvent) -> None: ...
    async def on_pnl_update(self, snapshot: PortfolioSnapshot) -> None:
        """Обновляет дневной PnL, drawdown, серию убытков; может дёрнуть CircuitBreaker."""
```

Полный список чеков — п. 7.2.

### 2.9. Execution / Auto-Trading

См. раздел 7. Публичные классы: `ModeController`, `OrderManager`, `PositionManager`, `CircuitBreaker`, `KillSwitch`, `PaperBroker`, `Reconciler`.

### 2.10. News & Sentiment

См. раздел 8. Публичные классы: `NewsFetcher` (по источнику), `NewsPipeline`, `SentimentScorer` (Protocol), `NewsReactionRules`.

### 2.11. Notification

```python
class Notifier(Protocol):
    async def send(self, msg: Notification) -> None: ...

class Notification(BaseModel):
    level: Literal["INFO", "WARN", "CRITICAL"]
    kind: str            # "signal", "order_filled", "cb_tripped", "kill_switch", "mode_changed"
    title: str
    body: str
    ref_ids: dict[str, str] = {}

class NotificationRouter:
    """Маршрутизация по level/kind: CRITICAL → все каналы; ошибки отправки
    не блокируют торговый контур (fire-and-forget с ретраями, отдельная очередь)."""
```

Каналы: `TelegramNotifier` (Bot API), `EmailNotifier` (SMTP через `aiosmtplib`), `WebhookNotifier` (HMAC-подпись тела).

### 2.12. Orchestrator / Scheduler

```python
class EventBus:
    async def publish(self, event: DomainEvent) -> None: ...
    def subscribe(self, event_type: type[E], handler: Callable[[E], Awaitable[None]]) -> None: ...

class Scheduler:
    def every(self, interval: timedelta, job: Callable[[], Awaitable[None]], *, name: str) -> None: ...
    def on_bar_close(self, timeframe: str, job: Callable[[datetime], Awaitable[None]]) -> None: ...

class Orchestrator:
    async def start(self) -> None:
        """Порядок: DB migrate → load settings → restore mode/CB state → adapters connect →
        reconcile orders/positions → start subscriptions → start scheduler → start API."""
    async def stop(self, *, reason: str) -> None: ...
```

Периодические задачи: `poll_bars` (на закрытии бара), `poll_news` (60 с), `reconcile` (30 с), `portfolio_snapshot` (60 с), `health_check` (15 с), `expire_signals` (5 с).

Обработчики событий выполняются последовательно **для одного символа** (per-symbol asyncio.Lock) — это гарантирует детерминированный порядок решений.

### 2.13. Interfaces

См. раздел 9.

---

## 3. Модель данных и SQLite

### 3.1. Общие правила

- `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;`
- Таблицы объявляются как `STRICT` (SQLite ≥ 3.37).
- Время — `INTEGER` (Unix ms, UTC). Суффикс `_ts`.
- Денежные значения в торговых таблицах (`orders`, `positions`, `portfolio_snapshots`) — `TEXT` с десятичной строкой (точный `Decimal`). В `ohlcv` — `REAL` (аналитика).
- Идентификаторы — ULID (`TEXT`, лексикографически сортируемы по времени).
- **Single writer:** все записи идут через `DbWriter` — одну asyncio-задачу с очередью; чтение — пул соединений. Это устраняет `database is locked` при конкурентной записи.
- Идемпотентная вставка: `INSERT ... ON CONFLICT(<natural key>) DO NOTHING` или `DO UPDATE` по явному правилу.
- Миграции — нумерованные SQL-файлы `src/augur/db/migrations/NNNN_name.sql`, таблица `schema_migrations` (DDL и правила — `VERSIONING.md` §2.2, §8).
- `audit_log` — append-only: триггеры запрещают `UPDATE`/`DELETE`.

### 3.2. DDL основных таблиц

```sql
-- 0001_core.sql — signals, orders, audit_log, mode_state, cb_state; ohlcv — 0002_ohlcv.sql;
-- news, fills, positions, portfolio_snapshots — миграциями своих этапов
CREATE TABLE ohlcv (
    venue       TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    timeframe   TEXT    NOT NULL,
    ts_open     INTEGER NOT NULL,          -- Unix ms UTC
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      REAL    NOT NULL,
    ingested_ts INTEGER NOT NULL,
    PRIMARY KEY (venue, symbol, timeframe, ts_open)
) STRICT, WITHOUT ROWID;
-- PK покрывает range-запросы: WHERE venue=? AND symbol=? AND timeframe=? AND ts_open BETWEEN ? AND ?

CREATE TABLE news (
    id            TEXT    PRIMARY KEY,     -- ULID
    source        TEXT    NOT NULL,        -- finnhub | newsapi | alphavantage
    external_id   TEXT,
    url           TEXT,
    content_hash  TEXT    NOT NULL,        -- sha256(normalized title + source domain)
    title         TEXT    NOT NULL,
    summary       TEXT,
    published_ts  INTEGER NOT NULL,
    fetched_ts    INTEGER NOT NULL,
    symbols_json  TEXT    NOT NULL DEFAULT '[]',
    sentiment     REAL,                    -- [-1, 1], NULL до скоринга
    sentiment_method TEXT,                 -- "rule:v1" | "fusion:<ref>" [→ FUSION SPEC]
    relevance     REAL
) STRICT;
CREATE UNIQUE INDEX ux_news_hash ON news(content_hash);
CREATE INDEX ix_news_published ON news(published_ts);

CREATE TABLE signals (
    id             TEXT    PRIMARY KEY,    -- ULID
    kind           TEXT    NOT NULL CHECK (kind IN ('strategy','final')),
    parent_ids_json TEXT   NOT NULL DEFAULT '[]', -- для final: strategy signal ids
    strategy       TEXT,                   -- name@version (для kind='strategy')
    params_hash    TEXT,
    symbol         TEXT    NOT NULL,
    timeframe      TEXT,
    action         TEXT    NOT NULL CHECK (action IN ('BUY','SELL','CLOSE','REDUCE','HOLD','NO_SIGNAL')),
    strength       REAL    NOT NULL CHECK (strength BETWEEN 0 AND 1),
    source         TEXT    NOT NULL CHECK (source IN ('classic','fusion','classic_fallback')),
    fused_signal_id TEXT   REFERENCES fused_signals(id),
    risk_flags_json TEXT   NOT NULL DEFAULT '[]',
    explanation_ref TEXT,
    stop_loss      TEXT,
    take_profit    TEXT,
    data_hash      TEXT,
    config_hash    TEXT    NOT NULL,
    code_version   TEXT    NOT NULL,       -- git sha
    bar_ts         INTEGER,                -- ts_open бара-триггера
    created_ts     INTEGER NOT NULL,
    expires_ts     INTEGER,
    status         TEXT    NOT NULL DEFAULT 'NEW'
                   CHECK (status IN ('NEW','SENT_TO_RISK','APPROVED','REJECTED','EXPIRED','EXECUTED'))
) STRICT;
-- идемпотентность: одна стратегия не может выдать два сигнала на один бар
CREATE UNIQUE INDEX ux_signals_strategy_bar ON signals(strategy, params_hash, symbol, timeframe, bar_ts)
    WHERE kind = 'strategy';
CREATE INDEX ix_signals_symbol_ts ON signals(symbol, created_ts);

CREATE TABLE orders (
    id               TEXT    PRIMARY KEY,  -- ULID
    client_order_id  TEXT    NOT NULL,     -- детерминированный, см. 7.3
    venue_order_id   TEXT,
    signal_id        TEXT    NOT NULL REFERENCES signals(id),
    risk_verdict_id  TEXT    NOT NULL,
    venue            TEXT    NOT NULL,
    symbol           TEXT    NOT NULL,
    side             TEXT    NOT NULL CHECK (side IN ('BUY','SELL')),
    type             TEXT    NOT NULL CHECK (type IN ('MARKET','LIMIT','STOP','STOP_LIMIT')),
    qty              TEXT    NOT NULL,
    limit_price      TEXT,
    stop_price       TEXT,
    filled_qty       TEXT    NOT NULL DEFAULT '0',
    avg_fill_price   TEXT,
    fee              TEXT    NOT NULL DEFAULT '0',
    fee_ccy          TEXT,
    state            TEXT    NOT NULL CHECK (state IN
                     ('PENDING','SUBMITTED','PARTIAL','FILLED','REJECTED','CANCELLED','UNKNOWN')),
    mode             TEXT    NOT NULL,     -- PAPER | MICRO_LIVE | FULL_AUTO
    reject_reason    TEXT,
    attempt          INTEGER NOT NULL DEFAULT 0,
    created_ts       INTEGER NOT NULL,
    updated_ts       INTEGER NOT NULL,
    version          INTEGER NOT NULL DEFAULT 0  -- optimistic locking
) STRICT;
CREATE UNIQUE INDEX ux_orders_coid ON orders(venue, client_order_id);
CREATE INDEX ix_orders_state ON orders(state) WHERE state IN ('PENDING','SUBMITTED','PARTIAL','UNKNOWN');
CREATE INDEX ix_orders_symbol_ts ON orders(symbol, created_ts);

CREATE TABLE fills (
    id             TEXT    PRIMARY KEY,
    order_id       TEXT    NOT NULL REFERENCES orders(id),
    venue_fill_id  TEXT    NOT NULL,
    qty            TEXT    NOT NULL,
    price          TEXT    NOT NULL,
    fee            TEXT    NOT NULL DEFAULT '0',
    ts             INTEGER NOT NULL
) STRICT;
CREATE UNIQUE INDEX ux_fills_venue ON fills(order_id, venue_fill_id);  -- дедуп при реконнекте

CREATE TABLE positions (
    venue          TEXT    NOT NULL,
    symbol         TEXT    NOT NULL,
    mode           TEXT    NOT NULL,
    qty            TEXT    NOT NULL,       -- знак = направление
    avg_price      TEXT    NOT NULL,
    realized_pnl   TEXT    NOT NULL DEFAULT '0',
    unrealized_pnl TEXT    NOT NULL DEFAULT '0',
    stop_loss      TEXT,
    take_profit    TEXT,
    opened_ts      INTEGER,
    updated_ts     INTEGER NOT NULL,
    PRIMARY KEY (venue, symbol, mode)
) STRICT;

CREATE TABLE portfolio_snapshots (
    ts             INTEGER NOT NULL,
    mode           TEXT    NOT NULL,
    equity         TEXT    NOT NULL,
    cash           TEXT    NOT NULL,
    margin_used    TEXT    NOT NULL DEFAULT '0',
    day_pnl        TEXT    NOT NULL,
    peak_equity    TEXT    NOT NULL,
    drawdown_pct   TEXT    NOT NULL,
    positions_json TEXT    NOT NULL,
    PRIMARY KEY (mode, ts)
) STRICT, WITHOUT ROWID;

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    actor       TEXT    NOT NULL,          -- "system" | "cli:<user>" | "api:<token_id>" | module name
    event       TEXT    NOT NULL,          -- см. перечень ниже
    severity    TEXT    NOT NULL CHECK (severity IN ('DEBUG','INFO','WARN','ERROR','CRITICAL')),
    ref_type    TEXT,                      -- signal | order | fused_signal | mode | cb | news
    ref_id      TEXT,
    payload_json TEXT   NOT NULL DEFAULT '{}',
    prev_hash   TEXT,                      -- hash-chain для обнаружения подмены
    row_hash    TEXT    NOT NULL
) STRICT;
CREATE INDEX ix_audit_ts ON audit_log(ts);
CREATE INDEX ix_audit_ref ON audit_log(ref_type, ref_id);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TABLE mode_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    mode            TEXT    NOT NULL,
    changed_ts      INTEGER NOT NULL,
    changed_by      TEXT    NOT NULL,
    micro_first_order_confirmed INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE TABLE cb_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    state           TEXT    NOT NULL CHECK (state IN ('ARMED','TRIPPED','KILLED')),
    reason          TEXT,
    tripped_ts      INTEGER,
    reset_by        TEXT,
    reset_ts        INTEGER
) STRICT;
```

Перечень `audit_log.event` (enum в коде): `MODE_CHANGE_REQUESTED`, `MODE_CHANGED`, `SIGNAL_CREATED`, `FUSION_FALLBACK`, `FUSION_INVALID`, `RISK_APPROVED`, `RISK_REJECTED`, `ORDER_SUBMITTED`, `ORDER_UPDATED`, `ORDER_DUPLICATE_SUPPRESSED`, `CB_TRIPPED`, `CB_RESET`, `KILL_SWITCH`, `RECONCILE_MISMATCH`, `DATA_ANOMALY`, `NEWS_REACTION`, `CONFIG_LOADED`, `ADAPTER_ERROR`.

### 3.3. Интерфейсные таблицы для Fusion

Только то, что нужно Signal Engine / Risk / Execution для аудита. Таблицы `ml_models`, `ml_predictions`, `llm_calls` — `[→ FUSION SPEC]`, здесь **не создаются**.

```sql
-- 0001_core.sql (фрагмент)
-- Контракт fused_signal — ML_LLM_FUSION_SPEC.md §4.6 + api_version; action хранится после
-- документированного маппинга Fusion → SignalAction (enter_long→BUY, enter_short→SELL,
-- exit→CLOSE, reduce→REDUCE, hold→HOLD, no_signal→NO_SIGNAL).
CREATE TABLE fused_signals (
    id                    TEXT    PRIMARY KEY,   -- ULID, выдаётся Signal Engine при приёме
    signal_id             TEXT    NOT NULL UNIQUE, -- UUID, выданный Fusion
    idempotency_key       TEXT    NOT NULL UNIQUE, -- защита от дублей ордеров
    api_version           TEXT    NOT NULL,      -- версия контракта, "1.0"; другая MAJOR → отклоняется
    asset                 TEXT    NOT NULL,
    timestamp             INTEGER NOT NULL,      -- close_time бара, Unix ms UTC
    expires_at            INTEGER NOT NULL,      -- Unix ms UTC
    action                TEXT    NOT NULL CHECK (action IN ('BUY','SELL','CLOSE','REDUCE','HOLD','NO_SIGNAL')),
    strength              REAL    NOT NULL CHECK (strength BETWEEN 0 AND 1),
    reason                TEXT    NOT NULL,
    explanation_ref       TEXT    NOT NULL,      -- строка-ссылка, НЕ FK (FUSION SPEC §4.6); NOT NULL с 0003_fusion_contract
    risk_flags_json       TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(risk_flags_json)),
    source_breakdown_json TEXT    NOT NULL CHECK (json_valid(source_breakdown_json)),  -- {ml, tech, llm, rules_fired}
    fusion_config_version TEXT    NOT NULL,      -- версия конфига весов/порогов, "fusion-2026.11.1"
    valid                 INTEGER NOT NULL CHECK (valid IN (0,1)),
    validation_errors_json TEXT   NOT NULL DEFAULT '[]',
    created_at            INTEGER NOT NULL DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER))  -- Unix ms UTC
) STRICT;
CREATE INDEX ix_fused_asset_ts ON fused_signals(asset, timestamp);
CREATE INDEX ix_fused_created ON fused_signals(created_at);

-- Схема decision_audit — VERSIONING.md §6.1 (единственный источник истины)
CREATE TABLE decision_audit (
    id                  TEXT    PRIMARY KEY,                 -- ULID
    signal_id           TEXT    NOT NULL REFERENCES signals(id),
    fused_signal_id     TEXT    REFERENCES fused_signals(id),  -- NULL при fusion.enabled=false (classic / classic_fallback)
    order_id            TEXT    REFERENCES orders(id),       -- NULL до создания ордера и для решений без ордера
    app_version         TEXT    NOT NULL CHECK (length(app_version) > 0
                                                AND app_version NOT LIKE '%+unknown'),
    strategy_version    TEXT    NOT NULL CHECK (length(strategy_version) > 0),
    ml_model_version    TEXT    NOT NULL CHECK (length(ml_model_version) > 0),
    llm_prompt_version  TEXT    NOT NULL CHECK (length(llm_prompt_version) > 0),
    ml_prediction_id    TEXT,                                -- ULID ml_predictions.id; логическая ссылка без FK (см. примечание)
    llm_status          TEXT,                                -- ok | rejected | fallback_ml_only; NULL, если LLM не участвовал
    risk_verdict        TEXT    NOT NULL CHECK (risk_verdict IN ('APPROVED', 'REDUCED', 'REJECTED')),
    risk_reason         TEXT,                                -- причина REJECTED / REDUCED
    config_hash         TEXT    NOT NULL,
    mode                TEXT    NOT NULL CHECK (mode IN ('ANALYSIS_ONLY','SIGNAL_ONLY','PAPER','MICRO_LIVE','FULL_AUTO')),
    execution_result    TEXT,                                -- JSON; NULL до отправки, заполняется один раз
    explanation         TEXT,                                -- асинхронный LLM explain; заполняется один раз
    created_at          INTEGER NOT NULL DEFAULT (CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER))  -- Unix ms UTC
) STRICT;

-- Append-only: удаление запрещено; order_id, execution_result, explanation — write-once (NULL → значение);
-- остальные поля неизменяемы
CREATE TRIGGER decision_audit_no_delete BEFORE DELETE ON decision_audit
BEGIN SELECT RAISE(ABORT, 'decision_audit is append-only'); END;

CREATE TRIGGER decision_audit_immutable BEFORE UPDATE ON decision_audit
WHEN NEW.signal_id          IS NOT OLD.signal_id
  OR NEW.fused_signal_id    IS NOT OLD.fused_signal_id
  OR NEW.app_version        IS NOT OLD.app_version
  OR NEW.strategy_version   IS NOT OLD.strategy_version
  OR NEW.ml_model_version   IS NOT OLD.ml_model_version
  OR NEW.llm_prompt_version IS NOT OLD.llm_prompt_version
  OR NEW.ml_prediction_id   IS NOT OLD.ml_prediction_id
  OR NEW.llm_status         IS NOT OLD.llm_status
  OR NEW.risk_verdict       IS NOT OLD.risk_verdict
  OR NEW.risk_reason        IS NOT OLD.risk_reason
  OR NEW.config_hash        IS NOT OLD.config_hash
  OR NEW.mode               IS NOT OLD.mode
  OR NEW.created_at         IS NOT OLD.created_at
  OR (OLD.order_id         IS NOT NULL AND NEW.order_id         IS NOT OLD.order_id)
  OR (OLD.execution_result IS NOT NULL AND NEW.execution_result IS NOT OLD.execution_result)
  OR (OLD.explanation      IS NOT NULL AND NEW.explanation      IS NOT OLD.explanation)
BEGIN SELECT RAISE(ABORT, 'decision_audit row is immutable'); END;
```

Правила заполнения и порядок записи относительно отправки ордера — `VERSIONING.md` §6.1–6.2. `ml_prediction_id` — логическая ссылка на `ml_predictions.id` без `REFERENCES`: таблица создаётся миграцией ML-контура (0.10.0), а FK на несуществующую таблицу в SQLite ломает любой INSERT в дочернюю.

> Невалидные fused_signal тоже сохраняются (`valid=0`) — это источник данных для CB-триггера «серия невалидных» (п. 7.4) и для разбора инцидентов.

### 3.4. Типовые запросы и индексы

| Запрос | Индекс |
|---|---|
| Бары за диапазон | PK `ohlcv(venue,symbol,timeframe,ts_open)` |
| Активные ордера для реконсиляции | частичный `ix_orders_state` |
| Цепочка решения по сигналу | PK `fused_signals(id)`, UNIQUE `fused_signals(signal_id)`, `ix_audit_ref` |
| Последние N fused_signal (для streak) | `ix_fused_created` |
| Дневной PnL | PK `portfolio_snapshots(mode, ts)` |
| Новости за окно | `ix_news_published` |

### 3.5. Репозитории

```python
class SignalRepository:
    async def insert_strategy_signal(self, s: StrategySignal) -> bool: ...   # False = дубликат
    async def insert_final(self, s: SignalReady, *, config_hash: str, code_version: str) -> None: ...
    async def set_status(self, signal_id: str, status: str) -> None: ...

class OrderRepository:
    async def insert_pending(self, o: OrderIntent) -> Literal["inserted", "duplicate"]: ...
    async def transition(self, order_id: str, new_state: OrderState, *,
                         expected_version: int, **fields) -> None:
        """Проверяет допустимость перехода (7.3) и optimistic lock."""
    async def open_orders(self) -> list[OrderRecord]: ...

class AuditRepository:
    async def append(self, event: AuditEvent) -> None:
        """Вычисляет row_hash = sha256(prev_hash + canonical_json(row))."""
    async def chain_for_signal(self, signal_id: str) -> DecisionChain: ...
```

---

## 4. Интеграция с рынками

### 4.1. Унифицированный протокол адаптера

```python
# src/augur/adapters/base.py
class OrderRequest(BaseModel, frozen=True):
    client_order_id: str
    symbol: str                 # каноническое имя
    side: Literal["BUY", "SELL"]
    type: Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT"]
    qty: Decimal
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: Literal["GTC", "IOC", "FOK", "DAY"] = "GTC"
    reduce_only: bool = False
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None

class OrderAck(BaseModel, frozen=True):
    client_order_id: str
    venue_order_id: str | None
    state: OrderState
    filled_qty: Decimal = Decimal(0)
    avg_price: Decimal | None = None
    raw: dict = {}              # сырой ответ (без секретов) для аудита

class AccountState(BaseModel, frozen=True):
    equity: Decimal; cash: Decimal; margin_used: Decimal; currency: str; ts: int  # Unix ms UTC

class InstrumentSpec(BaseModel, frozen=True):
    symbol: str; qty_step: Decimal; min_qty: Decimal; min_notional: Decimal
    price_tick: Decimal; contract_size: Decimal = Decimal(1)
    trading_hours: str | None = None   # для акций/форекс

class MarketAdapter(Protocol):
    venue: str
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def get_historical(self, symbol: str, timeframe: str,
                             start: int, end: int) -> list[Bar]: ...  # Unix ms UTC, start <= ts_open < end
    async def get_latest(self, symbol: str) -> Tick: ...
    async def place_order(self, req: OrderRequest) -> OrderAck: ...
    async def cancel_order(self, client_order_id: str, symbol: str) -> OrderAck: ...
    async def get_order(self, client_order_id: str, symbol: str) -> OrderAck | None: ...
    async def get_positions(self) -> list[VenuePosition]: ...
    async def get_account_state(self) -> AccountState: ...
    async def get_instrument(self, symbol: str) -> InstrumentSpec: ...
    async def market_status(self, symbol: str) -> MarketStatus: ...   # open/closed/halted
```

`get_order` (поиск по `client_order_id`) — ключевой метод для идемпотентности и восстановления после реконнекта. Если площадка не поддерживает такой поиск нативно, адаптер эмулирует его (см. MT5).

### 4.2. Политика вызовов (обязательна для Execution)

```python
class CallPolicy(BaseModel):
    timeout_s: float = 10.0
    max_retries: int = 3
    backoff_base_s: float = 0.5        # экспонента + jitter
    retry_on: tuple[type[Exception], ...] = (NetworkError, RateLimitError, TimeoutError)

class ResilientAdapter:
    """Декоратор над MarketAdapter: таймауты, retry, rate-limit, классификация ошибок,
    метрики. Execution работает ТОЛЬКО с ResilientAdapter."""
```

Критическое правило: **`place_order` при таймауте не ретраится вслепую.** Сначала `get_order(client_order_id)`; если ордер найден — используем его; если нет — повторная отправка с тем же `client_order_id`. Если статус не удаётся установить — ордер переводится в `UNKNOWN`, новые ордера по символу блокируются до реконсиляции.

Классификация ошибок: `RetryableError` (сеть, 5xx, rate limit) / `RejectedError` (недостаточно средств, неверный объём — без retry) / `FatalAdapterError` (авторизация — сразу триггер CB).

### 4.3. Форекс — MetaTrader 5

| Метод адаптера | MT5 API |
|---|---|
| `connect` | `mt5.initialize(login=..., password=..., server=...)`, проверка `mt5.last_error()` |
| `get_historical` | `mt5.copy_rates_range(symbol, TIMEFRAME_xx, date_from, date_to)` |
| `get_latest` | `mt5.symbol_info_tick(symbol)` |
| `place_order` | `mt5.order_check(request)` → `mt5.order_send(request)`; `retcode == TRADE_RETCODE_DONE` |
| `cancel_order` | `order_send({"action": TRADE_ACTION_REMOVE, "order": ticket})` |
| `get_order` | поиск по `comment` в `mt5.orders_get()` и `mt5.history_orders_get(...)` |
| `get_positions` | `mt5.positions_get()` |
| `get_account_state` | `mt5.account_info()` |
| `get_instrument` | `mt5.symbol_info(symbol)` (`volume_step`, `volume_min`, `trade_tick_size`) |

Особенности:
- Пакет `MetaTrader5` работает **только на Windows** с запущенным терминалом. `[ДОПУЩЕНИЕ]` На Linux — отдельный Windows-хост/VM с тонким RPC-сервисом (`mt5-bridge`), реализующим тот же протокол `MarketAdapter`.
- API синхронный и не потокобезопасный → все вызовы в одном выделенном потоке (`ThreadPoolExecutor(max_workers=1)`).
- Нет нативного `client_order_id`: используем `comment` (≤ 31 символа; `client_order_id` формируется короче — см. 7.3) и фиксированный `magic` для нашего бота.
- Время баров MT5 — во времени сервера брокера; адаптер конвертирует в UTC по смещению из конфигурации `mt5_server_utc_offset` `[ДОПУЩЕНИЕ: смещение задаётся вручную, т.к. API не отдаёт его надёжно]`.

### 4.4. Криптобиржи — CCXT

| Метод адаптера | CCXT |
|---|---|
| `connect` | `ccxt.async_support.<exchange>({apiKey, secret, enableRateLimit: True})`, `load_markets()` |
| `get_historical` | `fetch_ohlcv(symbol, timeframe, since, limit)` с пагинацией |
| `get_latest` | `fetch_ticker(symbol)` / `fetch_order_book` для спреда |
| `place_order` | `create_order(symbol, type, side, amount, price, params={"clientOrderId": coid})` |
| `cancel_order` | `cancel_order(id, symbol)` |
| `get_order` | `fetch_order(id, symbol)` либо `fetch_open_orders` + `fetch_closed_orders` с фильтром по `clientOrderId` |
| `get_positions` | spot: из `fetch_balance()`; деривативы: `fetch_positions()` |
| `get_account_state` | `fetch_balance()` |
| `get_instrument` | `market(symbol)` → `precision`, `limits` |

Особенности: имя параметра клиентского id различается по биржам (`clientOrderId` в унифицированном API, но часть бирж требует свои ключи) — маппинг в `ccxt_venue_quirks.py`. Округление объёма — `amount_to_precision`, цены — `price_to_precision`. Sandbox-режим (`set_sandbox_mode(True)`) используется в интеграционных тестах.

### 4.5. Акции — Alpaca

Библиотека `alpaca-py`.

| Метод адаптера | Alpaca |
|---|---|
| `connect` | `TradingClient(key, secret, paper=settings.alpaca_paper)`, `StockHistoricalDataClient` |
| `get_historical` | `StockHistoricalDataClient.get_stock_bars(StockBarsRequest(...))` |
| `get_latest` | `get_stock_latest_quote` |
| `place_order` | `submit_order(MarketOrderRequest / LimitOrderRequest(..., client_order_id=coid))` |
| `cancel_order` | `cancel_order_by_id(id)` |
| `get_order` | `get_order_by_client_id(coid)` |
| `get_positions` | `get_all_positions()` |
| `get_account_state` | `get_account()` |
| `market_status` | `get_clock()` (`is_open`), календарь `get_calendar` |

Alpaca нативно поддерживает `client_order_id` — эталон для идемпотентности. Paper-счёт Alpaca используется как «внешний» paper-контур для акций.

### 4.6. PaperBroker

Внутренний симулятор, реализующий `MarketAdapter`. Используется в режиме `PAPER` для всех рынков и как ядро исполнения в бэктесте (общая модель комиссий/проскальзывания — п. 6.3). Котировки берёт из реального адаптера (read-only).

---

## 5. Интерфейс с гибридным ML+LLM контуром (контракт, без деталей)

### 5.1. Разделение ответственности

| Вопрос | Этот документ | `ML_LLM_FUSION_SPEC.md` |
|---|---|---|
| Формат входа/выхода Fusion | ✅ | ссылается сюда |
| Поведение Signal Engine при сбое Fusion | ✅ | — |
| Прохождение fused_signal через Risk Gate | ✅ | — |
| Таблицы `fused_signals`, `decision_audit` | ✅ | читает |
| ML-модели, фичи, обучение | — | ✅ |
| LLM-провайдеры, промпты, кэширование | — | ✅ |
| Правила/веса слияния, variance filter | — | ✅ |
| Защита от галлюцинаций, семантическая валидация | — | ✅ |
| Таблицы `ml_models`, `ml_predictions`, `llm_calls` | — | ✅ |
| Skills `hybrid-ml-llm-fusion`, `llm-output-validation` | — | ✅ |

### 5.2. Вход Fusion (`FusionRequest`)

```json
{
  "$id": "fusion_request",
  "type": "object",
  "required": ["asset", "timeframe", "timestamp", "idempotency_key"],
  "additionalProperties": false,
  "properties": {
    "asset":           {"type": "string", "minLength": 1},
    "timeframe":       {"type": "string"},
    "timestamp":       {"type": "integer", "minimum": 0, "description": "close_time закрытого бара, Unix ms UTC. Fusion НЕ должен использовать данные позже timestamp"},
    "idempotency_key": {"type": "string", "minLength": 1, "description": "sha256(asset | bar_close_ts | strategy_version | params_hash); возвращается в FusedSignal без изменений"},
    "context": {
      "type": "object",
      "description": "по умолчанию {}; Signal Engine передаёт сигнал стратегии",
      "properties": {
        "strategy_signal_id": {"type": "string", "description": "ULID StrategySignal"},
        "strategy_version":   {"type": "string", "pattern": "^[a-z0-9_]+@\\d+\\.\\d+\\.\\d+$", "description": "name@SemVer, напр. sma_cross@1.0.0"},
        "action":             {"enum": ["BUY", "SELL", "CLOSE", "REDUCE", "HOLD", "NO_SIGNAL"]},
        "strength":           {"type": "number", "minimum": 0, "maximum": 1},
        "params_hash":        {"type": "string", "description": "sha256 канонического JSON параметров стратегии"},
        "data_hash":          {"type": "string", "description": "sha256 окна последних warmup_bars баров"}
      }
    }
  }
}
```

Python-модель — `augur.fusion.client.FusionRequest` (Pydantic, `extra="forbid"`, frozen). Новости (п. 8.3) и позиция в 0.4.0 не передаются.

### 5.3. Выход Fusion (`FusedSignal`)

> **Схема fused_signal** определена в ML_LLM_FUSION_SPEC.md §4.6.
> Этот раздел описывает только потребительские правила: fallback, TTL, поведение при недоступном Fusion.

Поля, типы и ограничения здесь не дублируются (`api_version`, `signal_id`, `idempotency_key`, `asset`, `timestamp`, `expires_at`, `action`, `strength`, `reason`, `explanation_ref`, `risk_flags`, `source_breakdown`, `fusion_config_version` — см. FUSION SPEC §4.6). Для потребителя существенно:

- `action` — уже `SignalAction` (6 значений, маппинг — п. 2.7); `HOLD` и `NO_SIGNAL` неисполняемы.
- `expires_at` — абсолютный срок жизни, Unix ms UTC; `expires_at > timestamp`.
- `risk_flags` — список объектов `{code, severity}`, `severity ∈ {low, medium, high}`; флаги только ужесточают решение (п. 5.6, правило 5).
- `source_breakdown` — `{ml, tech, llm, rules_fired}`, значения `float | list[str] | dict`; требования к сумме нет.
- `explanation_ref` — непустая строка-ссылка, НЕ FK: `"fusion_stub"` (0.4.0) / `"decision_audit:<ulid>"` (0.5.0+).

Пример (`StubFusionClient`, 0.4.0):

```json
{
  "api_version": "1.0",
  "signal_id": "6a3247d4-f93b-56ad-88b6-d9c319a7d2fb",
  "idempotency_key": "3eb0ab8afb8741ba1df21036e5cc84b28efddeedefdf00e31f31c1f547af2d6b",
  "asset": "BTC/USDT",
  "timestamp": 1795161600000,
  "expires_at": 1795163400000,
  "action": "HOLD",
  "strength": 0.0,
  "reason": "fusion_stub",
  "explanation_ref": "fusion_stub",
  "risk_flags": [],
  "source_breakdown": {"ml": 0.0, "tech": 1.0, "llm": 0.0, "rules_fired": []},
  "fusion_config_version": "stub-0.4.0"
}
```

### 5.4. Python-контракт

```python
# src/augur/fusion/contract.py — поля и семантика: FUSION SPEC §4.6
class RiskFlag(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: str = Field(min_length=1)
    severity: Literal["low", "medium", "high"]

SourceBreakdownValue = float | list[str] | dict[str, Any]

class FusedSignal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    api_version: str                      # == augur.versions.API_VERSION, иначе ValidationError
    signal_id: str                        # UUID
    idempotency_key: str = Field(min_length=1)
    asset: str = Field(min_length=1)
    timestamp: int = Field(ge=0)          # Unix ms UTC
    expires_at: int = Field(ge=0)         # > timestamp (model_validator)
    action: SignalAction
    strength: float = Field(ge=0.0, le=1.0)
    reason: str
    explanation_ref: str                  # непустая строка-ссылка, НЕ FK
    risk_flags: list[RiskFlag]
    source_breakdown: dict[str, SourceBreakdownValue]   # {ml, tech, llm, rules_fired}
    fusion_config_version: str = Field(min_length=1)

# src/augur/fusion/client.py
class FusionClient(Protocol):
    async def get_fused_signal(self, request: FusionRequest) -> FusedSignal | None:
        """Идемпотентен по request.idempotency_key. None — сигнала нет (Signal Engine уходит в fallback)."""

class StubFusionClient:
    """0.4.0, без ML/LLM: HOLD, если в context есть strategy_signal_id, иначе NO_SIGNAL;
    strength=0, reason="fusion_stub", explanation_ref="fusion_stub", risk_flags=[],
    source_breakdown={"ml": 0.0, "tech": 1.0, "llm": 0.0, "rules_fired": []},
    fusion_config_version="stub-0.4.0", signal_id=uuid5(STUB_NAMESPACE, idempotency_key),
    expires_at = timestamp + половина длительности бара."""

class HttpFusionClient:
    """Клиент к внешнему Fusion-сервису (в 0.4.0 не реализован; реализация сервиса — [→ FUSION SPEC])."""
```

> `StubFusionClient` возвращает `HOLD` (или `NO_SIGNAL` без технического сигнала), а не копию классического сигнала, чтобы в MVP итоговое решение явно шло через ветку `classic_fallback` и было видно в аудите. `[ДОПУЩЕНИЕ]`. Альтернатива из ТЗ — «neutral fused_signal» — это именно HOLD.

### 5.5. Валидация на стороне Signal Engine (структурная)

Signal Engine выполняет **только структурную** валидацию; семантическая (галлюцинации, согласованность объяснения) — `[→ FUSION SPEC]`.

| # | Проверка | При провале |
|---|---|---|
| V1 | Pydantic-валидация `FusedSignal` по FUSION SPEC §4.6 (`extra=forbid`) — повторно, даже если клиент вернул готовый объект | invalid |
| V2 | `api_version == versions.API_VERSION` (валидатор V1) | invalid |
| V3 | `idempotency_key` совпадает с отправленным в `FusionRequest` | invalid |
| V4 | `asset` совпадает с отправленным в `FusionRequest` | invalid |
| V5 | `expires_at > now` | invalid (`expired`) |
| V6 | ответ получен: клиент не бросил исключение (в т.ч. таймаут) и не вернул `None` | unavailable |
| V7 | `action ∈ {BUY, SELL}` при отсутствии хотя бы одного согласного классического сигнала → добавить флаг `fusion_contrarian` (не invalid) `[ДОПУЩЕНИЕ]` | flag |

invalid → `FUSION_INVALID` (`WARN`) в `audit_log` с перечнем ошибок; unavailable → `FUSION_FALLBACK` (`WARN`). Невалидный сигнал не исправляется.

### 5.6. Правила поведения

```python
async def resolve_signal(self, strat_signals, ctx) -> SignalReady:
    classic = self.combine_classic(strat_signals)
    if not self.settings.fusion.enabled:
        return classic.with_source("classic")
    try:
        fused = await asyncio.wait_for(self.fusion.get_fused_signal(req), timeout=self.settings.fusion.timeout_ms / 1000)
        fused, errors = (self.validate_fused(fused, req, now_ms) if fused is not None   # V1–V5
                         else (None, ["fusion returned no signal"]))                  # V6
    except (FusionUnavailable, asyncio.TimeoutError, ValidationError) as e:
        fused, errors = None, [repr(e)]
    if fused is None or errors:
        await self.repo.save_fused(fused, req, valid=False, errors=errors)   # для streak
        await self.audit.append(event=event, severity="WARN", ...)          # FUSION_INVALID | FUSION_FALLBACK (п. 5.5)
        await self.bus.publish(FusionInvalid(idempotency_key=req.idempotency_key, errors=errors))
        return classic.with_source("classic_fallback").add_flag("ml_disabled")
    fused_id = await self.repo.save_fused(fused, req, valid=True, errors=[])
    return SignalReady.from_fused(fused, fused_id=fused_id, parents=strat_signals)
```

Нормативно:
1. Signal Engine **MUST NOT** импортировать ML-библиотеки (`sklearn`, `torch`, `lightgbm`, …) и LLM SDK (`anthropic`, `openai`, …). Проверка — `import-linter` + CI-grep.
2. Если Fusion недоступен/невалиден → режим **ML-disabled** (только классика) + `WARN` в `audit_log`.
3. `fused_signal` **MUST** пройти Risk Gate. Ни один путь кода не ведёт из `src/augur/fusion` в `src/augur/execution` (контракт `import-linter`).
4. `expires_at` из fused_signal (Unix ms UTC) — срок жизни сигнала; Signal Engine отбрасывает `expires_at <= now` (V5), Risk Gate отклоняет просроченные повторно (чек `signal_ttl`).
5. Fusion не может повышать режим, менять лимиты или отключать чеки — его `risk_flags` (`{code, severity}`) могут только **ужесточать** решение (см. 7.2, чек `risk_flags`).

---

## 6. Стратегии и бэктестинг

### 6.1. Формат стратегии

См. интерфейс `Strategy` в п. 2.5. Обязательные атрибуты: `name`, `version` (SemVer), `params_model` (Pydantic), `warmup_bars`; методы `on_bar`, опционально `on_tick`, `generate_signals`.

Пример:

```python
class SmaCrossParams(BaseModel):
    fast: int = Field(10, ge=2); slow: int = Field(30, ge=3)
    atr_period: int = 14; atr_stop_mult: float = 2.0

@registry.register
class SmaCross(Strategy):
    name = "sma_cross"; version = "1.0.0"
    params_model = SmaCrossParams
    warmup_bars = 60

    def generate_signals(self, ctx: StrategyContext) -> list[StrategySignal]:
        """BUY при пересечении fast SMA снизу вверх slow SMA; SELL — наоборот.
        Стоп = close ∓ atr_stop_mult × ATR."""
```

### 6.2. Классические стратегии (стартовый набор)

| Стратегия | Тип | Рынки | Этап |
|---|---|---|---|
| `sma_cross` | тренд | все | MVP |
| `donchian_breakout` | пробой | крипта, форекс | 4 |
| `rsi_mean_reversion` | возврат к среднему | акции, форекс | 4 |
| `bollinger_squeeze` | волатильность | крипта | 4 |
| `macd_trend_filter` | тренд с фильтром | все | 4 |

Индикаторы — собственная реализация на `numpy`/`pandas` или `pandas-ta` `[ДОПУЩЕНИЕ]`; версия библиотеки пинится, т.к. влияет на детерминизм. ML- и fusion-стратегии — `[→ FUSION SPEC]`.

### 6.3. Backtest Engine

Событийный (bar-by-bar), использует **тот же** `SignalEngine` → `RiskGate` → `PaperBroker`, что и live, с подменой часов и источника данных.

```python
class BacktestConfig(BaseModel):
    strategy: str; params: dict
    symbols: list[str]; timeframe: str
    start: datetime; end: datetime
    initial_equity: Decimal = Decimal("10000")
    fee_model: FeeModelConfig            # maker/taker bps, фикс. за лот, своп (форекс)
    slippage_model: SlippageModelConfig  # fixed_bps | spread_half | volume_impact
    fill_on: Literal["next_open", "next_close"] = "next_open"
    fusion_mode: Literal["disabled", "cached"] = "disabled"
    seed: int = 42

class BacktestResult(BaseModel):
    run_id: str; config_hash: str; data_hash: str; code_version: str
    metrics: BacktestMetrics
    equity_curve: list[tuple[datetime, Decimal]]
    trades: list[TradeRecord]

class BacktestEngine:
    async def run(self, cfg: BacktestConfig) -> BacktestResult: ...
    async def walk_forward(self, cfg: BacktestConfig, train: timedelta, test: timedelta) -> list[BacktestResult]: ...
```

Защита от look-ahead bias:
1. Стратегия на баре `t` видит только бары с `ts_open ≤ t` (контекст отдаёт срез, а не весь DataFrame).
2. Исполнение сигнала с бара `t` — не раньше открытия бара `t+1` (`fill_on="next_open"` по умолчанию).
3. Индикаторы считаются инкрементально или на срезе — тест `test_no_lookahead` сравнивает сигналы при полном и обрезанном наборе данных: они должны совпадать на общем префиксе.
4. Новости в бэктесте фильтруются по `published_ts ≤ t` (и с задержкой `news_latency_s`, по умолчанию 60 с).

Модели издержек (общие для бэктеста и `PaperBroker`):

| Модель | Формула |
|---|---|
| `fixed_bps` | `fill = price × (1 ± bps/1e4)` |
| `spread_half` | `fill = mid ± spread/2` (исторический спред или конфиг) |
| `volume_impact` | `fill = price × (1 ± k × sqrt(qty / bar_volume))` |
| Комиссия | `notional × fee_bps/1e4` или фикс. за лот; своп за ночь для форекс |

Метрики (`BacktestMetrics`): total return, CAGR, **Sharpe** (annualized, rf=0 по умолчанию), **Sortino**, **max drawdown** (% и длительность), **win rate**, **profit factor**, avg win/loss, expectancy, число сделок, exposure, turnover, издержки в % от PnL.

LLM в бэктесте: `fusion_mode="disabled"` (по умолчанию) или `"cached"` — Fusion отвечает из кэша по `request_id`/хешу входа; промах кэша = ошибка прогона (не живой вызов). Детали кэша — `[→ FUSION SPEC]`.

Результаты бэктестов сохраняются в `backtest_runs` (id, config_json, config_hash, data_hash, code_version, metrics_json, created_ts) — `[ДОПУЩЕНИЕ: дополнительная таблица, не в списке ТЗ]`.

---

## 7. Автоторговля (Execution Layer)

### 7.1. Режимы

```python
class ModeController:
    async def current(self) -> TradingMode: ...
    async def request_change(self, target: TradingMode, *, actor: str) -> ModeChangeTicket:
        """Проверяет: target ≤ settings.max_allowed_mode; переход на 1 шаг вверх или любой вниз;
        CB не в TRIPPED/KILLED; для FULL_AUTO — выполнены promotion-критерии.
        Возвращает тикет с одноразовым кодом (6 цифр, TTL 120 с), пишет MODE_CHANGE_REQUESTED."""
    async def confirm_change(self, ticket_id: str, code: str, *, actor: str) -> TradingMode:
        """Атомарно: UPDATE mode_state + audit MODE_CHANGED. При ошибке — режим не меняется."""
    async def downgrade(self, target: TradingMode, *, actor: str, reason: str) -> None: ...
```

| Режим | Особенности исполнения |
|---|---|
| `PAPER` | Адаптер = `PaperBroker`; полный Risk Gate; ордера в `orders` с `mode='PAPER'` |
| `MICRO_LIVE` | Реальный адаптер; `qty` принудительно = `max(min_qty, notional ≤ micro_live_max_notional)`; **первый ордер после входа в режим** ожидает ручного подтверждения (`augur orders approve <id>` или кнопка GUI, подтверждаемая кодом); остальные — автоматически |
| `FULL_AUTO` | Реальный адаптер; автоматическое исполнение в пределах `RiskLimits` |

При старте процесса режим восстанавливается из `mode_state`, но **не выше** `settings.trading_mode`; если конфиг ниже сохранённого — понижение с записью в аудит.

### 7.2. Pre-trade risk checks

Порядок фиксирован (детерминизм), выполняются все чеки (не short-circuit), чтобы в аудите был полный список причин.

| # | Имя чека | Условие прохождения | Источник данных |
|---|---|---|---|
| R1 | `mode_allows_trading` | режим ∈ {PAPER, MICRO_LIVE, FULL_AUTO} | ModeController |
| R2 | `circuit_breaker_armed` | `cb_state.state == ARMED` | CircuitBreaker |
| R3 | `signal_ttl` | `now_utc < sig.expires_at` | SignalReady |
| R4 | `action_actionable` | `action ∉ {HOLD, NO_SIGNAL}` | SignalReady |
| R5 | `no_unknown_orders` | нет ордеров в `UNKNOWN` по символу | orders |
| R6 | `market_open` | `market_status == OPEN`, не halt | adapter.market_status |
| R7 | `abnormal_volatility` | `|z(ATR или return)| < volatility_halt_zscore` | Data Layer |
| R8 | `spread` | `spread_bps ≤ max_spread_bps` | latest tick |
| R9 | `liquidity` | 24h объём / глубина стакана ≥ `min_liquidity_quote` | adapter |
| R10 | `daily_loss` | `day_pnl > −max_daily_loss_pct × equity_start_of_day` | portfolio_snapshots |
| R11 | `max_open_positions` | открытых позиций < лимита (для открывающих ордеров) | positions |
| R12 | `concentration` | `(экспозиция символа + новый notional) / equity ≤ max_symbol_concentration_pct` | positions |
| R13 | `position_size` | `0 < sized_qty`, `notional ≤ max_position_pct_equity × equity`, ≥ `min_qty`/`min_notional` | PositionSizer, InstrumentSpec |
| R14 | `margin_available` | достаточно cash/маржи | account_state |
| R15 | `risk_flags` | ни один флаг не входит в `blocking_flags` (конфиг; напр. `news_halt`, `fusion_contrarian` в MICRO_LIVE) | SignalReady |
| R16 | `news_pause` | стратегия/символ не приостановлены новостным контуром | NewsReactionRules |
| R17 | `duplicate_intent` | нет активного ордера с тем же направлением по тому же сигналу | orders |
| R18 | `micro_live_cap` | в MICRO_LIVE notional ≤ `micro_live_max_notional` | config |

Флаги могут **уменьшать** размер: `soft_flags` (напр. `news_mixed`, `high_vol`) → `sized_qty × risk_flag_multiplier` (по умолчанию 0.5) `[ДОПУЩЕНИЕ]`. Такой вердикт записывается как `decision_audit.risk_verdict='REDUCED'`; без уменьшения — `'APPROVED'`.

При провале любого чека: `RiskVerdict.approved=False`, `signals.status='REJECTED'`, `decision_audit.risk_verdict='REJECTED'`, `audit_log: RISK_REJECTED` с полным списком `CheckResult` в `payload_json`.

Пример `RiskVerdict`:

```json
{
  "verdict_id": "01JD3XA0...",
  "signal_id": "01JD3X9Z...",
  "approved": false,
  "mode": "PAPER",
  "sized_qty": null,
  "checks": [
    {"name": "signal_ttl", "passed": true, "value": "2026-11-20T09:12:04Z", "limit": "2026-11-20T09:17:00Z"},
    {"name": "spread", "passed": false, "value": "41.2", "limit": "30", "reason": "spread_bps above limit"}
  ],
  "evaluated_at": "2026-11-20T09:12:04.551Z"
}
```

### 7.3. Исполнение

**Идемпотентность.**

```python
def make_client_order_id(signal_id: str, leg: str = "entry", attempt: int = 0) -> str:
    """Детерминированный id: 'jn' + base32(sha256(f"{signal_id}|{leg}|{attempt}"))[:22] → 24 символа.
    Влезает в MT5 comment (≤31) и в лимиты бирж (обычно ≤32–36). Тот же сигнал → тот же id."""
```

- Вставка в `orders` по `UNIQUE(venue, client_order_id)`: повторная обработка того же сигнала (рестарт, дубль события) даёт `duplicate` → `ORDER_DUPLICATE_SUPPRESSED`, отправки на биржу нет.
- Повторная отправка после таймаута — **с тем же** `client_order_id`, предварительно `get_order()`.
- `attempt` увеличивается только при явном перевыставлении (например, после `REJECTED` из-за precision) — это новый ордер.

**Машина состояний ордера.**

```
            ┌─────────┐ place_order ok  ┌───────────┐ partial fill ┌─────────┐
  create ──▶│ PENDING │────────────────▶│ SUBMITTED │─────────────▶│ PARTIAL │──┐
            └────┬────┘                 └─────┬─────┘              └────┬────┘  │ fill rest
                 │ reject (pre-send            │ fill                    │       ▼
                 │ or venue reject)            ▼                         │  ┌────────┐
                 ├──────────────▶ REJECTED   FILLED ◀────────────────────┘  │ FILLED │
                 │                             ▲                            └────────┘
                 │ timeout, status unknown     │ reconcile
                 └──────────────▶ UNKNOWN ─────┴──▶ SUBMITTED/PARTIAL/FILLED/REJECTED/CANCELLED
  SUBMITTED / PARTIAL ── cancel ──▶ CANCELLED   (для PARTIAL — filled_qty сохраняется)
```

```python
ALLOWED_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    PENDING:   {SUBMITTED, REJECTED, UNKNOWN},
    SUBMITTED: {PARTIAL, FILLED, REJECTED, CANCELLED, UNKNOWN},
    PARTIAL:   {PARTIAL, FILLED, CANCELLED, UNKNOWN},
    UNKNOWN:   {SUBMITTED, PARTIAL, FILLED, REJECTED, CANCELLED},
    FILLED: set(), REJECTED: set(), CANCELLED: set(),
}
```

```python
class OrderManager:
    async def handle_verdict(self, v: RiskVerdict, sig: SignalReady) -> OrderRecord | None:
        """approved → build OrderIntent → insert_pending (idempotent) →
        [MICRO_LIVE first order: await manual approval] → submit."""
    async def submit(self, order: OrderRecord) -> OrderRecord: ...
    async def on_venue_update(self, update: VenueOrderUpdate) -> None:
        """Применяет переход; fills дедуплицируются по (order_id, venue_fill_id)."""
    async def cancel(self, order_id: str, *, reason: str) -> None: ...
    async def cancel_all(self, *, reason: str) -> int: ...

class Reconciler:
    async def run(self) -> ReconcileReport:
        """Каждые 30 с и при реконнекте: сверяет open orders и позиции БД с площадкой.
        UNKNOWN → фактический статус. Расхождение позиций > qty_step → RECONCILE_MISMATCH,
        CRITICAL-нотификация, блок новых ордеров по символу до ручного разбора."""
```

**Частичные исполнения:** `PARTIAL` хранит `filled_qty`; позиция обновляется по каждому fill. Для MARKET-ордеров с остатком по таймауту `partial_timeout_s` (60 с) остаток отменяется `[ДОПУЩЕНИЕ]`. Стоп/тейк выставляются на фактически исполненный объём.

**Реконнекты:** при восстановлении соединения — сначала `Reconciler.run()`, затем подписки; ордера, отправленные в момент обрыва, переводятся в `UNKNOWN` до сверки.

**Логирование:** каждый переход → `orders` (UPDATE с `version+1`) + `audit_log` (`ORDER_UPDATED`). `decision_audit.order_id` и `decision_audit.execution_result` заполняются один раз (`VERSIONING.md` §6.2).

### 7.4. Circuit breaker и kill switch

```python
class CBTrigger(StrEnum):
    CONSECUTIVE_LOSSES = "consecutive_losses"
    DRAWDOWN = "drawdown"
    DAILY_LOSS = "daily_loss"
    ADAPTER_ERRORS = "adapter_errors"
    FUSION_INVALID_STREAK = "fusion_invalid_streak"
    RECONCILE_MISMATCH = "reconcile_mismatch"
    MANUAL = "manual"

class CircuitBreaker:
    state: Literal["ARMED", "TRIPPED", "KILLED"]
    async def evaluate(self, metrics: RiskMetrics) -> None: ...
    async def trip(self, trigger: CBTrigger, detail: str) -> None:
        """1) state=TRIPPED (persist); 2) запрет новых ордеров (R2);
        3) cancel_all открытых ордеров на вход; 4) режим → PAPER-hold (позиции НЕ закрываются автоматически,
        если не включено cb.flatten_on_trip); 5) audit CB_TRIPPED; 6) CRITICAL notify."""
    async def reset(self, *, actor: str, confirm_code: str) -> None:
        """Только из CLI с кодом. После reset режим = PAPER; повторная эскалация — через ModeController."""

class KillSwitch:
    async def engage(self, *, actor: str, flatten: bool = True) -> KillReport:
        """Немедленно: state=KILLED; cancel_all; при flatten=True — закрыть все позиции рыночными
        reduce_only ордерами (через адаптер, минуя сигнальный контур, но с записью в аудит);
        режим → ANALYSIS_ONLY."""
```

| Триггер | Условие (по умолчанию) | Параметр |
|---|---|---|
| Серия убытков | N подряд закрытых сделок с PnL < 0, N = 4 | `max_consecutive_losses` |
| Просадка | `(peak − equity)/peak > 10%` | `max_drawdown_pct` |
| Дневной убыток | `day_pnl < −3%` | `max_daily_loss_pct` |
| Ошибки адаптера | ≥ 5 ошибок `Retryable` за 5 мин или 1 `FatalAdapterError` | `cb.adapter_error_window` |
| **Невалидные fused_signal** | ≥ `fusion.max_invalid_streak` (5) подряд с `valid=0` **или** > 50% invalid за последние 20 запросов | `fusion.max_invalid_streak`, `fusion.invalid_ratio` |
| Расхождение реконсиляции | любое расхождение позиций в live | — |

Для триггера Fusion: детали того, *что* считается невалидным семантически, — `[→ FUSION SPEC]`; CircuitBreaker считает по `fused_signals.valid` и по событию `FusionInvalid`. `[ДОПУЩЕНИЕ]` Мягкая реакция до CB: после 3 invalid подряд Signal Engine сам переходит в ML-disabled на `fusion.cooldown_s` (300 с).

Kill switch доступен: CLI `augur kill-switch --flatten`, GUI (кнопка + ввод кода), опционально Telegram-команда `/kill` с проверкой chat_id `[ДОПУЩЕНИЕ]`. Kill switch **не требует** кода подтверждения на включение (скорость важнее), только на сброс.

После срабатывания: `augur cb reset --confirm` → режим `PAPER` → эскалация заново через `mode set`.

### 7.5. Аудит и воспроизводимость

Для каждого авто-ордера в `decision_audit` есть: `signal_id`, `fused_signal_id` (NULL без fusion), `order_id`, `app_version`, `strategy_version`, `ml_model_version`, `llm_prompt_version`, `ml_prediction_id`, `llm_status`, `risk_verdict` + `risk_reason`, `config_hash`, `mode`, `execution_result`, `explanation` (схема — `VERSIONING.md` §6.1).

Цепочка воспроизведения:

```
ohlcv (data_hash) ─▶ signals[kind=strategy] (strategy@version, params_hash, bar_ts)
                 ─▶ fused_signals (fusion_request_id → [→ FUSION SPEC] ml_predictions/llm_calls)
                 ─▶ signals[kind=final] (source, risk_flags, expires_ts)
                 ─▶ decision_audit (версии, risk_verdict)
                 ─▶ orders ─▶ fills ─▶ positions
```

```python
class ReplayService:
    async def explain(self, signal_id: str) -> DecisionChain:
        """Собирает цепочку из таблиц выше + audit_log."""
    async def replay(self, signal_id: str) -> ReplayDiff:
        """Перезапускает стратегии на тех же барах (проверка data_hash), классическую агрегацию
        и RiskGate с сохранённым контекстом; Fusion — из сохранённого fused_signal (без живого вызова).
        Возвращает расхождения. Ожидание: пустой diff при совпадающих code_version и config_hash."""
```

CLI: `augur audit explain <signal_id>`, `augur audit replay <signal_id>`, `augur audit verify-chain` (проверка hash-chain `audit_log`).

---

## 8. Новостной контур и реакция

### 8.1. Источники

| Источник | Что берём | Привязка к символам | Примечание |
|---|---|---|---|
| Finnhub | company news, general/forex/crypto news, экономический календарь | `related` тикеры | rate limit бесплатного плана; ключ `FINNHUB_API_KEY` |
| NewsAPI | заголовки по ключевым словам | по словарю алиасов | бесплатный план — только для разработки `[проверить лицензию]` |
| Alpha Vantage | `NEWS_SENTIMENT` (с готовым sentiment) | `ticker_sentiment` | можно использовать как «внешний» скоринг |

### 8.2. Пайплайн

```
fetch (per source, every 60s) ─▶ normalize ─▶ dedup (content_hash + url) ─▶ symbol mapping
   ─▶ sentiment scoring (SentimentScorer) ─▶ persist(news) ─▶ publish news_event ─▶ reactions
```

```python
class NewsFetcher(Protocol):
    source: str
    async def fetch(self, since: datetime) -> list[RawNewsItem]: ...

class SentimentScorer(Protocol):
    name: str                                   # "rule:v1" | "fusion:<...>"
    async def score(self, item: NewsItem) -> SentimentScore: ...   # [-1, 1], confidence

class RuleBasedScorer:      # словарь позитивных/негативных терминов + веса источников; детерминирован
    ...

class NewsPipeline:
    async def run_once(self) -> int: ...
```

Скоринг через LLM — реализация `SentimentScorer` в Fusion-модуле `[→ FUSION SPEC]`. Этот документ требует только: если LLM-скорер недоступен → fallback на `RuleBasedScorer`, поле `sentiment_method` отражает фактический метод.

### 8.3. Контракт `news_event`

```json
{
  "$id": "news_event/1.0",
  "type": "object",
  "required": ["event_id", "news_id", "symbols", "published_at", "sentiment", "sentiment_method", "source"],
  "properties": {
    "event_id":        {"type": "string"},
    "news_id":         {"type": "string"},
    "symbols":         {"type": "array", "items": {"type": "string"}},
    "published_at":    {"type": "string", "format": "date-time"},
    "sentiment":       {"type": "number", "minimum": -1, "maximum": 1},
    "confidence":      {"type": "number", "minimum": 0, "maximum": 1},
    "sentiment_method":{"type": "string"},
    "source":          {"type": "string"},
    "title":           {"type": "string", "maxLength": 300},
    "importance":      {"enum": ["low", "medium", "high"]}
  }
}
```

Потребители: Fusion (вход `news_events`), `NewsReactionRules`, GUI. Новостной контур **не** создаёт ордеров и сигналов на вход.

### 8.4. Реакции

```yaml
# config/news_rules.yaml
rules:
  - name: strong_negative_pause
    when: {sentiment_lt: -0.6, confidence_gte: 0.6, importance_in: [high]}
    action: pause_symbol            # R16 блокирует открытие новых позиций
    duration_min: 60
  - name: mixed_reduce_risk
    when: {abs_sentiment_lt: 0.2, importance_in: [high], window_count_gte: 3}
    action: add_risk_flag           # soft flag "news_mixed" → уменьшение размера
    flag: news_mixed
  - name: macro_event_blackout
    when: {macro_event: [FOMC, NFP, CPI], minutes_before: 15, minutes_after: 15}
    action: pause_symbol
    scope: affected_currencies
  - name: any_high_alert
    when: {importance_in: [high]}
    action: notify
```

Действия: `pause_symbol`, `pause_strategy`, `add_risk_flag`, `reduce_risk(multiplier)`, `notify`. Каждое применение → `audit_log: NEWS_REACTION`. Реакции могут только **ужесточать** риск.

---

## 9. GUI и CLI

### 9.1. Веб-GUI (FastAPI + WebSocket)

Фронтенд `[ДОПУЩЕНИЕ]`: серверный рендер Jinja2 + HTMX + lightweight-charts — минимум JS-сборки, удобно для агентной генерации. Альтернатива — отдельный SPA (вне скоупа MVP).

| Страница | Содержимое | Действия |
|---|---|---|
| Dashboard | equity curve, дневной PnL, режим, состояние CB, health адаптеров/Fusion | — |
| Strategies | список, параметры, статус (active/paused), последние сигналы | включить/выключить стратегию (в SIGNAL_ONLY/PAPER) |
| Backtest | форма запуска, прогресс (WS), метрики, equity, сделки | запуск, сравнение прогонов |
| Positions | открытые позиции, ордера, fills | отмена ордера; закрыть позицию (с кодом) |
| News | лента, sentiment, активные реакции/паузы | снять паузу (с аудитом) |
| Auto-Trading Control | режим, лимиты, CB, kill switch, очередь MICRO_LIVE-подтверждений, decision chain | запрос смены режима, kill switch, approve первого ордера |
| **Models / LLM Audit** | отдельная страница, предусматривается в навигации; содержимое — `[→ FUSION SPEC]` | — |

REST (префикс `/api/v1`, Bearer-токен):

| Метод | Путь | Описание |
|---|---|---|
| GET | `/status` | режим, CB, health |
| GET | `/signals?symbol=&since=` | сигналы |
| GET | `/signals/{id}/chain` | decision chain |
| GET | `/orders`, `/positions`, `/portfolio/snapshots` | торговые данные |
| POST | `/backtests` · GET `/backtests/{id}` | бэктесты |
| GET | `/news?symbol=` | новости |
| POST | `/mode/requests` | запрос смены режима (подтверждение — CLI) |
| POST | `/orders/{id}/approve` | подтверждение первого MICRO_LIVE-ордера (тело: код) |
| POST | `/kill-switch` | kill switch |
| GET | `/fusion/health` | статус FusionClient |

WebSocket `/ws/events`: сообщения `{"type": "signal_ready" | "risk_verdict" | "order_update" | "position_update" | "news_event" | "cb_state" | "mode_changed" | "backtest_progress", "data": {...}}`.

API по умолчанию слушает `127.0.0.1`. Доступ извне — только через reverse proxy с TLS `[ДОПУЩЕНИЕ]`.

### 9.2. CLI (Typer)

Имя команды — `augur`, точка входа — `augur.interfaces.cli.main:app` (`[project.scripts]` в `pyproject.toml`).

```
augur run [--mode-check]                         # запуск оркестратора
augur backtest --strategy sma_cross --symbol BTC/USDT --tf 1h --from 2024-01-01 --to 2025-01-01 [--params params.json]
augur signals list [--symbol] [--since]
augur signals explain <signal_id>
augur news fetch|list [--symbol]
augur config validate                            # проверка .env/конфига для текущего режима
augur mode show
augur mode set <MODE> --confirm                  # запрос + ввод одноразового кода
augur auto-trade enable                          # = mode set PAPER→... по шагам; алиас
augur auto-trade disable                         # downgrade до SIGNAL_ONLY
augur orders list|cancel <id>|approve <id>
augur kill-switch [--flatten/--no-flatten]
augur cb status|reset --confirm
augur audit explain|replay <signal_id> | verify-chain
augur db migrate|backup
augur data backfill --symbol --tf --days
```

### 9.3. Общие DTO

Пакет `src/augur/interfaces/dto.py` — единственный источник моделей для GUI и CLI: `StatusDTO`, `SignalDTO`, `DecisionChainDTO`, `OrderDTO`, `PositionDTO`, `PortfolioSnapshotDTO`, `BacktestRequestDTO`, `BacktestResultDTO`, `NewsDTO`, `ModeChangeRequestDTO`, `KillSwitchRequestDTO`. CLI использует сервисный слой напрямую (in-process) либо HTTP-клиент к запущенному процессу (`--remote`) — с теми же DTO.

---

## 10. Безопасность и надёжность

| Область | Требование |
|---|---|
| Секреты | Только `.env`/переменные окружения/OS keyring; `.env` в `.gitignore`; `SecretStr`; pre-commit `gitleaks` |
| Ключи биржи | Создаются с правами **trade-only, без вывода средств**, с IP-whitelist; доступны только `src/augur/execution` и `src/augur/adapters` (контракт `import-linter`: Settings с ключами передаётся адаптеру при сборке в composition root) |
| LLM | В промпты/ответы не попадают ключи, `client_order_id`, балансы счёта сверх необходимого `[→ FUSION SPEC]` для деталей; Fusion-процесс не получает ключи бирж |
| Валидация | Все входящие данные (адаптеры, новости, Fusion, API) — Pydantic с `extra="forbid"` там, где формат наш |
| API | Bearer-токен, bind на localhost, rate limit, CORS выключен, подтверждение опасных действий кодом |
| Аудит | Каждое решение и ордер → `audit_log` (append-only, hash-chain) |
| Логи | `structlog` JSON; фильтр маскирует поля `*key*`, `*secret*`, `*token*`, `password` |
| Надёжность | Graceful shutdown (отмена входных ордеров по флагу), restore state при старте, реконсиляция, watchdog; `db backup` ежедневно (`sqlite3 .backup`) |
| Время | Проверка дрейфа часов (NTP) при старте; при дрейфе > 2 с — запрет live-режимов `[ДОПУЩЕНИЕ]` |
| Kill switch / CB | Обязательны, покрыты интеграционными тестами, нельзя отключить конфигом в live-режимах |
| Зависимости | Пины версий (`uv.lock`), `pip-audit` в CI |

---

## 11. Структура проекта

```
augur/
├── pyproject.toml                 # uv/hatch, ruff, mypy --strict, pytest, import-linter
├── .env.example                   # все переменные БЕЗ значений
├── AGENTS.md                      # правила для Codex
├── CLAUDE.md                      # правила для Claude Code
├── .claude/skills/                # skills (раздел 13)
├── .codex/skills/                 # те же skills для Codex (симлинк/копия)
├── config/
│   ├── default.yaml
│   └── news_rules.yaml
├── docs/
│   ├── ARCHITECTURE.md            # этот документ
│   └── ML_LLM_FUSION_SPEC.md
├── src/augur/
│   ├── config/        settings.py, hashing.py
│   ├── db/            connection.py, writer.py, migrations/0001_core.sql, 0002_fusion_contract.sql, repositories/
│   ├── data/          models.py, service.py, normalizer.py, validators.py
│   ├── adapters/      base.py, resilient.py, mt5.py, ccxt_adapter.py, ccxt_venue_quirks.py, alpaca.py, paper.py
│   ├── strategies/    base.py, registry.py, indicators.py, sma_cross.py, ...
│   ├── backtest/      engine.py, costs.py, metrics.py, walk_forward.py
│   ├── signals/       engine.py, combine.py, events.py
│   ├── fusion/        contract.py, client.py (Protocol + HttpFusionClient), stub.py   # ТОЛЬКО контракт + заглушка
│   ├── risk/          gate.py, checks/, sizing.py, post_trade.py
│   ├── execution/     modes.py, order_manager.py, state_machine.py, idempotency.py,
│   │                  circuit_breaker.py, kill_switch.py, reconciler.py, positions.py
│   ├── news/          fetchers/{finnhub,newsapi,alphavantage}.py, pipeline.py, scoring.py, reactions.py
│   ├── notify/        router.py, telegram.py, email.py, webhook.py
│   ├── orchestrator/  bus.py, scheduler.py, app.py, composition_root.py
│   ├── audit/         replay.py, chain.py
│   └── interfaces/
│       ├── dto.py
│       ├── api/       main.py, routes/, ws.py, templates/
│       └── cli/       main.py, commands/
└── tests/
    ├── unit/          # стратегии, чеки, state machine, валидатор контракта
    ├── property/      # hypothesis: инварианты позиций/PnL, no-lookahead
    ├── integration/   # PaperBroker, CCXT sandbox, Alpaca paper, SQLite
    ├── contract/      # JSON-схемы fusion_request/fused_signal/news_event
    └── e2e/           # сценарии: CB trip, kill switch, duplicate signal, reconnect
```

> `src/augur/fusion/` — **тонкий интерфейсный модуль**: только контракт `FusedSignal`/`FusionRequest`, `FusionClient` Protocol, `HttpFusionClient` и `StubFusionClient`. Реализация — по `ML_LLM_FUSION_SPEC.md` (отдельный сервис/пакет).

Контракты `import-linter` (фрагмент):

```toml
[[tool.importlinter.contracts]]
name = "Signal Engine does not import ML/LLM"
type = "forbidden"
source_modules = ["augur.signals", "augur.risk", "augur.execution", "augur.strategies"]
forbidden_modules = ["sklearn", "torch", "lightgbm", "xgboost", "anthropic", "openai", "transformers"]

[[tool.importlinter.contracts]]
name = "Fusion cannot reach execution"
type = "forbidden"
source_modules = ["augur.fusion"]
forbidden_modules = ["augur.execution", "augur.adapters", "augur.risk"]

[[tool.importlinter.contracts]]
name = "Only risk gate feeds execution"
type = "forbidden"
source_modules = ["augur.signals", "augur.news", "augur.strategies"]
forbidden_modules = ["augur.execution"]
```

---

## 12. Требования к агентной разработке (Claude Code / Codex)

### 12.1. Код

- `mypy --strict`, `ruff` (включая `D` — docstrings, Google style), `pytest` с покрытием ≥ 85% для `risk/`, `execution/`, `signals/`, `fusion/contract.py`.
- Каждый публичный класс/функция — type hints + docstring с разделами Args/Returns/Raises и, для торговой логики, **Invariants**.
- Никаких `float` для денег в `risk/` и `execution/` (правило ruff/semgrep).
- Каждая задача агента завершается `make check` = `ruff + mypy + lint-imports + pytest`.

### 12.2. CLAUDE.md / AGENTS.md (общее содержимое)

Источник истины — `CLAUDE.md` в корне репозитория; `AGENTS.md` (Codex) — его копия. Ниже — его нормативная часть дословно; при расхождении побеждает `CLAUDE.md`.

````markdown
## Жёсткие правила

1. LLM не исполняет ордера и не меняет риск-лимиты. LLM только даёт сигнал/обоснование.
2. Все LLM-выходы проходят Pydantic-валидацию и variance filter. Невалидный выход = отсутствие сигнала.
3. Fusion не обходит Risk Gate. Любой путь к Execution идёт только через Risk Gate.
4. Ордер без `app_version`, `strategy_version`, `ml_model_version`, `llm_prompt_version` не исполняется.
5. Перед генерацией ML-кода, LLM-клиентов, fusion или risk — использовать соответствующий skill.
6. Секреты только через `.env` (Pydantic Settings). Никогда в коде, тестах, логах, коммитах.
7. Один этап = одна сессия. Между сессиями — `/clear`.
8. Режим исполнения по умолчанию — `PAPER`. `MICRO_LIVE` / `FULL_AUTO` включаются только явной конфигурацией.
9. Любое изменение схемы БД — только через миграцию с записью в `schema_migrations`.

## Команды

```bash
pip install -e ".[dev]"     # установка для разработки
pytest                      # тесты
ruff check .                # линтер
ruff format .               # форматирование
mypy src                    # типы
augur --version             # версия приложения
augur backtest              # бэктест стратегии (заглушка до соответствующего этапа)
augur kill-switch           # аварийная остановка торговли (заглушка до этапа Risk Gate)
```

Перед коммитом: `ruff check .` и `pytest` должны проходить.

## Skills (`.claude/skills/`)

| Skill | Когда использовать |
|---|---|
| `hybrid-ml-llm-fusion` | логика `fused_signal`, variance filter, веса ML/LLM |
| `llm-output-validation` | клиенты LLM, промпты, Pydantic-схемы ответов, запись в `llm_calls` |
| `auto-trade-risk-gate` | лимиты, размер позиции, kill-switch, проверки перед Execution |
| `release-management` | bump версии, CHANGELOG, annotated tag |

Для ML-обучения и инференса отдельный skill будет создан перед 0.10.0.

Если нужного skill нет в `.claude/skills/` — остановись и сообщи, не пиши код «по памяти».
````

### 12.3. Защитные механизмы против «агент обошёл правило»

| Механизм | Что ловит |
|---|---|
| `import-linter` в CI | импорт ML/LLM вне Fusion; Fusion → Execution; прямой вызов адаптера из сигналов |
| semgrep-правило `no-direct-place-order` | `place_order(` вне `execution/` |
| CODEOWNERS на `risk/`, `execution/`, `src/augur/db/migrations/`, `CLAUDE.md`, `AGENTS.md` | изменения требуют ревью человека |
| Golden-тесты Risk Gate | таблица (вход → вердикт); изменение вердиктов = падение тестов |
| PR-шаблон с чекбоксом «skill auto-trade-risk-gate использован» + ссылка на чек-лист | процессный контроль |
| Hook Claude Code `PreToolUse` на Edit/Write в `src/augur/{risk,execution}` | напоминание/блок, если skill не загружен `[ДОПУЩЕНИЕ: реализуется скриптом в .claude/settings.json]` |

---

## 13. Skills для Claude Code / Codex

### 13.1. Внешние skills: `agiprolabs/claude-trading-skills`

Репозиторий существует (MIT); установка в Claude Code — через marketplace: `/plugin marketplace add agiprolabs/claude-trading-skills`, затем `/plugin install trading-skills@agiprolabs-claude-trading-skills`. Также поддерживается ручное копирование skill-папок в каталог skills агента (Claude Code, Codex и др.). Точечная установка одного skill — `npx skills add agiprolabs/claude-trading-skills --skill exit-strategies --agent claude-code`, ставит в `.claude/skills/` текущего проекта.

| Skill | Использование в этом проекте | Ограничение |
|---|---|---|
| `signal-classification` | только интерфейс к ML-классификации (формат классов/strength) | ML-код — не здесь `[→ FUSION SPEC]` |
| `feature-engineering` | только контракт данных (какие поля Data Layer отдаёт Fusion) | фичи считает Fusion |
| `exit-strategies` | стопы, тейки, трейлинг в классических стратегиях | — |
| `slippage-modeling` | модели издержек `backtest/costs.py` и `PaperBroker` | — |
| `trade-journal` | структура `TradeRecord`, отчёты по сделкам | — |
| `market-microstructure` | чеки спреда/ликвидности (R8, R9), выбор типа ордера | — |

`[ДОПУЩЕНИЕ / проверить]` Наличие `exit-strategies` и `trade-journal` в репозитории подтверждено; точные имена `signal-classification`, `feature-engineering`, `slippage-modeling`, `market-microstructure` перед установкой сверить со списком в репозитории. Репозиторий ориентирован в основном на крипту/DeFi — для MT5/Alpaca его skills применимы частично. Пинить коммит (`git checkout <sha>`) и ревьюить SKILL.md перед подключением (skill = исполняемые агентом инструкции).

Размещение: `.claude/skills/<name>/SKILL.md` и `.codex/skills/<name>/SKILL.md` `[ДОПУЩЕНИЕ: путь .codex/skills задан в ТЗ; актуальный путь для Codex проверить по его документации]`.

### 13.2. Собственный skill: `auto-trade-risk-gate`

Skills `hybrid-ml-llm-fusion` и `llm-output-validation` — в `ML_LLM_FUSION_SPEC.md`, здесь не дублируются.

```
.claude/skills/auto-trade-risk-gate/
├── SKILL.md
├── reference/
│   ├── checks.md              # таблица R1–R18 из п. 7.2 с инвариантами
│   ├── order_state_machine.md # п. 7.3
│   ├── circuit_breaker.md     # п. 7.4
│   └── modes.md               # п. 1.4, 7.1
├── templates/
│   ├── risk_check.py.tmpl     # шаблон нового чека + тест
│   └── golden_case.yaml.tmpl
└── scripts/
    └── verify_invariants.py   # запускает lint-imports, semgrep, golden-тесты risk/execution
```

`SKILL.md`:

```markdown
---
name: auto-trade-risk-gate
description: Use BEFORE creating or changing anything in src/augur/risk or src/augur/execution
  (pre-trade checks, position sizing, order state machine, idempotency, circuit breaker, kill switch,
  trading modes). Enforces the invariants of ARCHITECTURE.md sections 7.1–7.5.
---

# Auto-trade risk gate

## Non-negotiable invariants
1. The only path to an order: SignalReady → RiskGate.evaluate() → OrderManager.handle_verdict().
2. RiskGate runs ALL checks R1–R18 in fixed order and records every CheckResult.
   A new check is appended with a new id; existing checks are never removed or relaxed silently.
3. Fusion output and news reactions may only TIGHTEN risk (flags, size multipliers ≤ 1), never loosen.
4. client_order_id = make_client_order_id(signal_id, leg, attempt) — deterministic. Never random.
5. On submit timeout: get_order(client_order_id) first; resend only with the same id; else UNKNOWN.
6. State transitions only via ALLOWED_TRANSITIONS; every transition persists (version+1) and audits.
7. CircuitBreaker TRIPPED/KILLED blocks all new entry orders; reset needs CLI confirm code;
   after reset mode = PAPER.
8. Mode escalation: one step up, ≤ max_allowed_mode, CLI confirm code, audit MODE_CHANGED.
   MICRO_LIVE: first order needs manual approval; size capped by micro_live_max_notional.
9. Money is Decimal; time is UTC; no wall-clock reads inside checks (use injected Clock).

## Workflow
1. Read reference/checks.md and the relevant reference file.
2. Write/adjust golden cases (templates/golden_case.yaml.tmpl) BEFORE code.
3. Implement; add unit + e2e tests (CB trip, kill switch, duplicate signal, reconnect→UNKNOWN).
4. Run scripts/verify_invariants.py; it must pass.
5. In the PR description list which invariants were touched and why.

## Forbidden
- Calling adapter.place_order/cancel_order outside src/augur/execution.
- Flags or config options that disable checks in MICRO_LIVE/FULL_AUTO.
- try/except that swallows a failed check and approves.
- Importing ML/LLM libraries.
```

### 13.3. Правила подключения

| Задача агента | Обязательные skills |
|---|---|
| Любые изменения в `risk/`, `execution/`, режимах, CB, kill switch | `auto-trade-risk-gate` |
| Fusion-реализация (отдельный пакет) | `hybrid-ml-llm-fusion`, `llm-output-validation` `[→ FUSION SPEC]` |
| Контракт `src/augur/fusion/contract.py` | `auto-trade-risk-gate` (раздел про ужесточение) + сверка с `ML_LLM_FUSION_SPEC.md` |
| Стратегии | `exit-strategies` |
| Бэктест/издержки | `slippage-modeling`, `trade-journal` |
| Чеки спреда/ликвидности | `market-microstructure` + `auto-trade-risk-gate` |

---

## 14. План реализации

| # | Этап | Результат / критерий готовности |
|---|---|---|
| 0 | Каркас + config | репозиторий, `pyproject`, `make check`, `Settings`, `config validate`, CLAUDE.md/AGENTS.md, import-linter |
| 1 | Data layer + SQLite | миграции 0001/0002, `DbWriter`, `ohlcv` backfill с CCXT, валидаторы баров |
| 2 | Адаптеры | `MarketAdapter`, `ResilientAdapter`, CCXT (sandbox), `PaperBroker`; заглушки MT5/Alpaca |
| 3 | Классические стратегии + бэктест | `sma_cross`, `BacktestEngine`, издержки, метрики, тест no-lookahead |
| 4 | Интерфейс Fusion (заглушка) | `contract.py`, `StubFusionClient`, валидация V1–V6, fallback, contract-тесты JSON-схем |
| 5 | Risk Gate | R1–R18, sizing, golden-тесты; skill `auto-trade-risk-gate` |
| 6 | Execution (PAPER) | `OrderManager`, state machine, идемпотентность, Reconciler, CB, kill switch, e2e-тесты |
| 7 | MICRO_LIVE | `ModeController` с кодами, ручное подтверждение 1-го ордера, micro-cap; прогон на реальном счёте с минимальным депозитом |
| 8 | FULL_AUTO | promotion-критерии, мониторинг, алерты, runbook инцидентов |
| 9 | News | фетчеры, дедуп, `RuleBasedScorer`, `news_event`, реакции |
| 10 | Skills + полировка | установка внешних skills (с пином), hooks, GUI-страницы, MT5-bridge, Alpaca |

GUI и CLI развиваются параллельно с этапа 3 (минимальный Dashboard + CLI `backtest`).

**MVP (этапы 0–6, урезанно):**
- Рынок: крипта (CCXT, одна биржа, 1–2 пары, таймфрейм 1h).
- Стратегия: `sma_cross`.
- Fusion: `StubFusionClient` → всегда `HOLD` → итоговый сигнал из ветки `classic_fallback` (видно в аудите).
- Режим: только `PAPER` (`max_allowed_mode=PAPER`).
- Risk Gate: полный набор чеков, применимых к крипте.
- GUI: Dashboard, Positions, Auto-Trading Control (kill switch); CLI: `run`, `backtest`, `signals`, `kill-switch`, `config validate`.

ML- и LLM-этапы реализуются отдельно по `ML_LLM_FUSION_SPEC.md` и подключаются заменой `StubFusionClient` на `HttpFusionClient` (флаг `FUSION__ENABLED=true`, `FUSION__ENDPOINT=...`) без изменений Signal Engine.

---

## 15. Риски и открытые вопросы

1. **Автоторговля реальными деньгами.** Ошибка в sizing или state machine приводит к прямым убыткам. Меры: лестница режимов, micro-cap, CB, kill switch, golden-тесты, ключи без права вывода. Открыто: кто и как утверждает переход в `FULL_AUTO` (один человек или «four eyes»)?
2. **Агент обошёл skill / ослабил риск-контур.** Skill — это инструкция, а не гарантия. Меры: import-linter, semgrep, CODEOWNERS, golden-тесты, hooks. Остаточный риск: изменение самих тестов агентом — требуется ревью человека для `tests/golden/`.
3. **Дублирование ML/LLM-логики вне fusion-модуля.** Агент может «удобно» добавить LLM-вызов в news или signals. Меры: запрет импортов, `SentimentScorer` как Protocol, ревью. Открыто: где физически живёт Fusion-реализация — отдельный репозиторий или пакет в монорепо?
4. **Расхождение контракта `fused_signal` между двумя спецификациями.** Меры: JSON-схемы в одном месте (`docs/contracts/`), версия `contract_version`, contract-тесты по обе стороны. Открыто: владелец схемы.
5. **Лицензии и условия API.** NewsAPI бесплатный план — только для разработки; Finnhub/Alpha Vantage — лимиты и запрет перепродажи данных; MetaTrader 5 — условия конкретного брокера; правила бирж на автоматическую торговлю. Нужна проверка перед live.
6. **Регуляторные ограничения.** Зависят от юрисдикции пользователя и площадки: доступ к Alpaca (US-брокер) и ряду криптобирж ограничен для некоторых стран; налоговый учёт сделок; требования брокера к алготорговле. Спецификация не является юридической консультацией — требуется проверка юристом.
7. **MetaTrader 5 только под Windows.** Нужен Windows-хост и RPC-мост; отдельная точка отказа и задержки. Открыто: допустим ли такой деплой, или Форекс через другой API (например, REST-брокер)?
8. **SQLite как единственное хранилище.** Single-writer снимает блокировки, но ограничивает пропускную способность и не даёт HA. Для тиковых данных по многим символам может не хватить. Открыто: нужен ли путь миграции на PostgreSQL/TimescaleDB.
9. **Детерминизм vs. внешний мир.** Ответы площадок, частичные исполнения и LLM не детерминированы; воспроизводимость гарантируется только для цепочки «данные → решение», а не для исполнения. Replay Fusion — только из сохранённых результатов.
10. **Расхождение бэктеста и live.** Проскальзывание, задержки, недоступность ликвидности, funding/свопы. Меры: общий код `PaperBroker`/бэктест, walk-forward, сравнение paper vs. backtest за одинаковый период перед эскалацией режима.
11. **Внешние skills как вектор атаки (supply chain).** SKILL.md исполняется агентом с доступом к репозиторию. Меры: пин коммита, ревью, отсутствие ключей live-счетов в окружении агента.

---

*Конец документа. Детали ML/LLM-контура — `ML_LLM_FUSION_SPEC.md`.*
