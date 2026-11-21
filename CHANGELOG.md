# Changelog

Все значимые изменения проекта Augur документируются в этом файле.

Формат основан на [Keep a Changelog 1.1.0](https://keepachangelog.com/ru/1.1.0/),
проект придерживается [Semantic Versioning 2.0.0](https://semver.org/lang/ru/).

## [Unreleased]

## [0.1.0] - 2026-11-20

### Добавлено

- Каркас проекта и `pyproject.toml` (сборка через hatchling, версия — единый источник истины).
- Структура пакета `src/augur/` по слоям: Data Layer, Signal Engine (Fusion), Risk Gate, Execution, Interfaces.
- Конфигурация через Pydantic Settings с загрузкой из `.env`.
- SQLite-схема со всеми таблицами MVP, включая `ml_models`, `ml_predictions`, `llm_calls` и `decision_audit`
  с полями версий `app_version`, `strategy_version`, `ml_model_version`, `llm_prompt_version`.
- Таблица `schema_migrations` для учёта применённых миграций.
- CLI-заглушка на Typer: команда `augur --version`.
- Документация: `ARCHITECTURE.md`, `ML_LLM_FUSION_SPEC.md`, `VERSIONING.md`, `CLAUDE.md`.

### Безопасность

- Секреты загружаются только из `.env`; `.env` исключён из репозитория.
- API-ключи и токены не попадают в логи.