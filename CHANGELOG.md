# Changelog

Все значимые изменения проекта Augur документируются в этом файле.

Формат основан на [Keep a Changelog 1.1.0](https://keepachangelog.com/ru/1.1.0/),
проект придерживается [Semantic Versioning 2.0.0](https://semver.org/lang/ru/).

## [Unreleased]

## [0.5.0] - 2026-11-27

**Компоненты:** db_schema_version 3 → 4 · api_version 1.0 (без изменений) · config_version 1 → 2 · артефакты `sma_cross@1.0.0`, `rsi@1.0.0`; `ml_model_version` / `llm_prompt_version` = `n/a` (VERSIONING.md §2.3)

### Добавлено

- Risk Gate (`augur.risk`, ARCHITECTURE.md §2.8, §7.2): `RiskGate.evaluate(sig, ctx)` — чеки
  R1–R18 в фиксированном порядке, без short-circuit (в вердикте полный список `CheckResult`);
  чистая функция от `(SignalReady, RiskContext, RiskLimits)`, без I/O и системных часов;
  отсутствие данных → чек не проходит (fail-closed). Gate только уменьшает объём или отклоняет.
- Sizing: `PositionSizer` Protocol, `FixedFractionalSizer` (риск `risk_per_trade_pct` до стопа,
  без стопа — `max_position_pct_equity`); далее cap по позиции, micro-cap в MICRO_LIVE,
  soft-флаги (× `risk_flag_multiplier`, вердикт `REDUCED`), округление вниз до `qty_step`.
  CLOSE — вся позиция, REDUCE — `reduce_fraction`.
- `RiskVerdict.risk_verdict` (APPROVED / REDUCED / REJECTED) и `risk_reason` — расширение схемы
  ARCH §2.8 для `decision_audit`.
- `RiskContext` и `RiskContextBuilder`: режим из `mode_state` (не выше `settings.trading_mode`),
  `cb_state`, позиции текущего режима, дневной PnL из `portfolio_snapshots`, ордера `UNKNOWN`
  (R5) и активные намерения (R17); `MarketSnapshot` / `market_snapshot(adapter)` — статус рынка,
  mid-цена, спред (bps), инструмент, счёт; `return_zscore(bars)` для R7.
- `DecisionAuditWriter` (VERSIONING.md §6.1–6.2): каждый вердикт, включая REJECTED и HOLD/NO_SIGNAL,
  атомарно пишется в `decision_audit` + `signals.status` + `audit_log` (`RISK_APPROVED` /
  `RISK_REJECTED` с полным списком чеков). `assert_auditable` / `UnauditableOrderError`:
  без полного набора версий записи (и ордера) нет. Id записи — ULID из
  `explanation_ref="decision_audit:<ulid>"`, если он есть.
- `SignalReady` (ARCH §2.7) — вход Risk Gate: `combine_classic()` (конфликт BUY/SELL → HOLD,
  большинство, при равенстве — менее рискованное действие, TTL ½ бара), `from_fused()` с V7
  `fusion_contrarian` (medium), `with_source()`, `add_flag()`.
- `SignalEngine.resolve_signal()` (ARCH §5.6): classic при `fusion.enabled=false`; иначе Fusion
  с таймаутом и заранее выданным `decision_audit_id`; сбой / таймаут / невалидный выход →
  `classic_fallback` + `ml_disabled` (low) + `FUSION_FALLBACK` / `FUSION_INVALID`.
- `SignalRepository`: `signals` (strategy/final), `fused_signals` (valid=0/1), `set_status`.
- Миграция `0004_risk_state.sql` (`expand`): `positions`, `portfolio_snapshots` (DDL ARCH §3.2).
- Golden-тесты Risk Gate: `tests/golden/risk_gate/cases.json` (ожидания посчитаны вручную;
  изменение — только с ревью человека, ARCH §15 п.2).

### Изменено

- `RiskLimits` (config_version 2): `risk_per_trade_pct`, `blocking_flags`,
  `blocking_flags_by_mode` (по умолчанию MICRO_LIVE → `fusion_contrarian`), `soft_flags`,
  `risk_flag_multiplier` (0 < x < 1), `reduce_fraction`. Все поля с дефолтами — `.env` менять
  не нужно.
- `StubFusionClient`: `explanation_ref="decision_audit:<ulid>"`, если в `context` передан
  `decision_audit_id`; иначе по-прежнему `"fusion_stub"`.
- ARCHITECTURE.md §5.2: в `FusionRequest.context` описан `decision_audit_id`.
- VERSIONING.md §2.2: константы `DB_SCHEMA_VERSION = 4`, `CONFIG_VERSION = 2`.
- `DB_SCHEMA_VERSION`: 3 → 4; `CONFIG_VERSION`: 1 → 2.

### Примечания

- Действия, снижающие риск (CLOSE/REDUCE), не блокируются чеками нового риска (R7, R10–R12,
  R14–R16, R18) и проходят R2 при CB `TRIPPED` (не `KILLED`) — `[ДОПУЩЕНИЕ]`, по skill
  `auto-trade-risk-gate` («CB останавливает только новые входы»).
- Skill `auto-trade-risk-gate` в примере показывает short-circuit проверок; реализовано по
  ARCH §7.2 (все чеки, полный список причин). Семантика kill switch в skill
  (`--release --mode`) расходится с ARCH §7.4 — решается на этапе 0.6.0.
- `signals.code_version` пока заполняется версией приложения, а не git sha.
- CircuitBreaker, KillSwitch, ModeController, Execution — этап 0.6.0; Risk Gate читает их
  состояние из `cb_state` / `mode_state`.

## [0.4.0] - 2026-11-23

**Компоненты:** db_schema_version 2 → 3 · api_version 1.0 (контракт применён) · config_version 1 (`CONFIG_VERSION`) · артефакты `sma_cross@1.0.0`, `rsi@1.0.0`

### Добавлено

- Fusion-контракт `FusedSignal` (Pydantic v2, `extra="forbid"`, frozen) по ML_LLM_FUSION_SPEC.md §4.6:
  проверки `api_version == versions.API_VERSION`, непустой `explanation_ref`, `signal_id` — UUID,
  `strength` ∈ [0, 1], `action` — 6 значений `SignalAction`, `expires_at > timestamp`;
  `RiskFlag(code, severity: low|medium|high)`.
- `FusionClient` Protocol, `FusionRequest`, `StubFusionClient` (без ML/LLM): HOLD при наличии
  технического сигнала, иначе NO_SIGNAL; `strength=0`, `explanation_ref="fusion_stub"`,
  `fusion_config_version="stub-0.4.0"`, TTL — половина бара; `signal_id = uuid5(idempotency_key)`.
- Signal Engine (`augur.signals`): `StrategyContext`, `StrategySignal` (ULID `signal_id`,
  `params_hash`, `data_hash` по окну `warmup_bars`), `SignalEngine.generate_signals()` и
  `async process()` — невалидный/просроченный выход Fusion отбрасывается с `FUSION_INVALID` в
  `audit_log`, сбой клиента — `FUSION_FALLBACK` (ML-disabled).
- `Strategy.generate_signals(ctx)` как обёртка над `on_bar`; `Strategy.strategy_version`.
- Встроенный генератор ULID `augur.ids` (без новых зависимостей).
- Миграция `0003_fusion_contract.sql`: `fused_signals` пересоздана с `explanation_ref NOT NULL`.
- `CONFIG_VERSION` в `versions.py`.

### Изменено

- **BREAKING:** `fused_signals.explanation_ref` — NOT NULL (миграция `0003_fusion_contract`,
  `compat: contract`). Путь миграции: `augur db migrate` (бэкап снимается автоматически); строк в
  `fused_signals` на 0.4.0 нет. Писатели обязаны передавать `"fusion_stub"` /
  `"decision_audit:<ulid>"`.
- **BREAKING:** формат `strategy_version` — `name@SemVer` (`sma_cross@1.0.0`, `rsi@1.0.0`) вместо
  `<имя>_v<N>`; `Strategy.version` — SemVer `X.Y.Z` (проверяется при регистрации). Записей
  `decision_audit` со старым форматом нет.
- Режимы PAPER/MICRO_LIVE/FULL_AUTO — единые имена во всех документах.
- VERSIONING.md §2.2/§2.3/§4 приведены к фактическим именам (`DB_SCHEMA_VERSION`, `API_VERSION`,
  `CONFIG_VERSION`) и фактическому составу 0.3.0/0.4.0.
- ML_LLM_FUSION_SPEC.md §4.6 объявлен источником истины схемы `fused_signal` (добавлены
  `api_version`, `action` после маппинга в `SignalAction`, `risk_flags` как `{code, severity}`);
  §7 — DDL по миграциям 0001 + 0003. ARCHITECTURE.md §5.3 ссылается на §4.6.
- `DB_SCHEMA_VERSION`: 2 → 3. `augur backtest` выводит `strategy_version`.

### Примечания

- `explanation_ref`: строка, НЕ FK. 0.4.0: `"fusion_stub"`; 0.5.0: `"decision_audit:<ulid>"`.
- Расхождение с ARCH §2.5/§6.1 закрыто: `generate_signals(ctx)` реализован как обёртка над `on_bar`.

## [0.3.1] - 2026-11-23

**Компоненты:** db_schema_version 2 (без изменений) · api_version 1.0 (константа, применение — 0.4.0) · артефакты —

### Добавлено

- Централизованные версии компонентов в `src/augur/versions.py` (`DB_SCHEMA_VERSION`, `API_VERSION`).
  Имена отличаются от VERSIONING.md §2.2 (`EXPECTED_DB_SCHEMA`, `FUSION_API_VERSION`) — документ
  выравнивается в Батче B.
- `SchemaVersionMismatchError` в `migrator.py` — защита от несоответствия БД и кода:
  файл миграции новее `DB_SCHEMA_VERSION` — отказ до изменения БД; схема после `migrate` ниже
  ожидаемой — «Примените миграции: augur db migrate»; БД новее кода с не-`expand` миграцией —
  «Обновите пакет augur». Лишние `expand`-миграции в БД по-прежнему допустимы (VERSIONING.md §2.2).
  Параметр `Migrator(expected_schema=...)`, по умолчанию `DB_SCHEMA_VERSION`.
- Тест `test_versions.py`: константа совпадает с последним файлом миграции, формат `API_VERSION`,
  сценарии `SchemaVersionMismatchError`.

### Изменено

- `augur --version` выводит db_schema и api: `augur 0.3.1 (db_schema: 2, api: 1.0)`.
- БД новее кода с `contract`-миграцией теперь даёт `SchemaVersionMismatchError` вместо
  `UnknownAppliedMigrationError` (оба — подклассы `MigrationError`; `UnknownAppliedMigrationError`
  остаётся для применённой `contract`-миграции без файла в пределах ожидаемой схемы).

## [0.3.0] - 2026-11-23

**Компоненты:** db_schema_version 2 (без изменений) · api_version — · артефакты `sma_cross_v1`, `rsi_v1`

### Добавлено

- Strategy Framework: базовый класс `Strategy` (`name`, `version`, `params_model`, `warmup_bars`,
  `on_bar(bar, history) -> Signal | None`), реестр (`register_strategy`, `get_strategy`,
  `list_strategies`), Pydantic `Signal` (`action` — 6 значений `SignalAction`, `strength` ∈ [0, 1],
  `reason`, `timestamp` в ms). Параметры валидируются `params_model`, неизвестные ключи отклоняются.
  `StrategySignal`/`StrategyContext` из ARCHITECTURE.md §2.5 — вместе с Signal Engine.
- Классические стратегии: SMA Crossover (`sma_cross_v1`, short=10/long=30), RSI (`rsi_v1`,
  период 14, 30/70, Уайлдер на окне 4 × period). Индикаторы — на чистом Python, только закрытые бары.
- Backtest Engine: bar-by-bar без look-ahead (стратегия видит только `data[:i]` и текущий закрытый бар),
  комиссия и проскальзывание в процентах, одна позиция на весь капитал (long/short без переворота,
  REDUCE — половина позиции), закрытие на последнем баре, equity curve по close.
  Исполнение `fill_on="close"` (по умолчанию) или `"next_open"` (ARCHITECTURE.md §6.3).
- Метрики (stdlib `math`): Total/Annualized Return, Sharpe, Sortino (rf=0), Max Drawdown, Win Rate,
  Profit Factor, число сделок, средняя сделка.
- CLI: `augur strategies list`, `augur backtest` (`--strategy --params --asset --timeframe --since
  --until --data-source paper --cash --commission --slippage --fill-on --seed`); данные — `PaperAdapter`,
  ордера не отправляются.

## [0.2.0] - 2026-11-23

**Компоненты:** db_schema_version 1 → 2 · api_version — · артефакты —

### Добавлено

- Протокол `MarketAdapter` (async) и DTO `OrderRequest`, `OrderAck`, `AccountState`, `InstrumentSpec`,
  `VenuePosition`, `OrderState`, `MarketStatus` по ARCHITECTURE.md §4.1; иерархия ошибок
  `AdapterError` → `RetryableError` / `RejectedError` / `FatalAdapterError`.
- DTO рыночных данных `Bar`, `Tick` (время — int ms UTC) и проверки целостности бара.
- Политика вызовов `CallPolicy` + `call_with_retry`: таймаут, экспоненциальная пауза с jitter,
  повтор только сетевых ошибок/таймаутов (ARCHITECTURE.md §4.2).
- `PaperAdapter`: детерминированные синтетические котировки без сети, мгновенное исполнение
  по текущей цене, идемпотентность по `client_order_id`.
- Адаптеры только для чтения: `CcxtAdapter` (крипто), `AlpacaAdapter` (акции), `Mt5Adapter` (Форекс);
  ордера через них — `NotImplementedError` до 0.7.0. SDK — опциональные extras `[ccxt]`, `[alpaca]`,
  `[mt5]`, импортируются лениво.
- Миграция `0002_ohlcv` (compat: expand, db_schema_version 2): таблица `ohlcv` (STRICT, PK по
  venue/symbol/timeframe/ts_open).
- `OhlcvLoader`: загрузка истории через адаптер с идемпотентным UPSERT; незакрытые бары не сохраняются,
  некорректные отбрасываются с записью `DATA_ANOMALY` в `audit_log` (hash-chain).
- Настройка `mt5_server_utc_offset` (часы) для перевода времени сервера MT5 в UTC.
- Мигратор `augur.db.migrator.Migrator` (VERSIONING.md §2.2, §8): forward-only, каждая миграция и её
  запись в `schema_migrations` — одна транзакция; при ошибке откатывается только упавшая миграция.
  checksum — sha256 с нормализацией CRLF→LF; изменённая применённая миграция → `ChecksumMismatchError`.
  Бэкап `VACUUM INTO backups/augur_pre_v<app>_schema<N>_<ts>.db` перед применением.
  БД новее кода допускается, только если лишние миграции `expand`.
- CLI: `augur db migrate`, `augur db status`, `augur db backup` (`--db`, по умолчанию `data/augur.db`).

### Изменено

- db_schema_version: 1 → 2 (миграция `0002_ohlcv`).
- ARCHITECTURE.md §2.3, §4.1: время в `Bar`, `Tick`, `AccountState` и диапазон `get_historical` — int ms UTC.
- VERSIONING.md §4: хранилище 0.2.0 — таблица `ohlcv` вместо `instruments`/`candles`.

## [0.1.0] - 2026-11-21

**Компоненты:** db_schema_version 1 · api_version — · артефакты —

### Добавлено

- Каркас проекта и `pyproject.toml` (сборка через hatchling, версия — единый источник истины).
- Пакет `src/augur/`: `config`, `db`, `interfaces/cli`; `augur.__version__` через `importlib.metadata`.
- Конфигурация через Pydantic Settings с загрузкой из `.env` (`augur.config.settings`, ARCHITECTURE.md §2.2):
  режим по умолчанию `PAPER`, `config_hash()` без секретов.
- Миграция `0001_core.sql` (compat: expand): `schema_migrations`, `artifacts`, `signals`, `fused_signals`,
  `orders`, `decision_audit`, `audit_log`, `mode_state`, `cb_state`. Все таблицы `STRICT`, время — INTEGER ms.
  `decision_audit` append-only, с обязательными `app_version`, `strategy_version`, `ml_model_version`,
  `llm_prompt_version`; `fused_signal_id` допускает NULL.
- `fused_signals` по контракту ML_LLM_FUSION_SPEC.md §4.6 + `api_version`; `signal_id` и `idempotency_key` — UNIQUE.
- CLI на Typer: `augur --version`.
- Тесты: версия пакета/CLI, применение миграции, STRICT, триггеры `decision_audit`.
- Документация: `ARCHITECTURE.md`, `ML_LLM_FUSION_SPEC.md`, `VERSIONING.md`, `CLAUDE.md`.

### Безопасность

- Секреты загружаются только из окружения/`.env` как `SecretStr`; `.env` исключён из репозитория,
  `.env.example` содержит только имена переменных без значений.