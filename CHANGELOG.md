# Changelog

Все значимые изменения проекта Augur документируются в этом файле.

Формат основан на [Keep a Changelog 1.1.0](https://keepachangelog.com/ru/1.1.0/),
проект придерживается [Semantic Versioning 2.0.0](https://semver.org/lang/ru/).

## [Unreleased]

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