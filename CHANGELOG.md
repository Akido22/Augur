# Changelog

Все значимые изменения проекта Augur документируются в этом файле.

Формат основан на [Keep a Changelog 1.1.0](https://keepachangelog.com/ru/1.1.0/),
проект придерживается [Semantic Versioning 2.0.0](https://semver.org/lang/ru/).

## [Unreleased]

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