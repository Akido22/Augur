# Changelog

Все значимые изменения проекта Augur документируются в этом файле.

Формат основан на [Keep a Changelog 1.1.0](https://keepachangelog.com/ru/1.1.0/),
проект придерживается [Semantic Versioning 2.0.0](https://semver.org/lang/ru/).

## [Unreleased]

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