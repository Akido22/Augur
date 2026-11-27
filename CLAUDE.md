# Проект: Augur — гибридная торговая система ML+LLM

Python 3.11+ · SQLite · MT5 (Форекс) · CCXT (крипто) · Alpaca (акции) · FastAPI + WebSocket · Typer.
Текущая версия: см. `pyproject.toml` (единый источник истины, SemVer).

## Спецификации (читать ДО генерации кода)

- `ARCHITECTURE.md` — слои, модули, план этапов, риски.
- `ML_LLM_FUSION_SPEC.md` — ML+LLM fusion, поля `fused_signal`, таблицы `ml_models` / `ml_predictions` / `llm_calls`.
- `VERSIONING.md` — SemVer, три уровня версий, CHANGELOG, git-политика, воспроизводимость ордеров.

Если задача затрагивает модуль, описанный в спецификации, — сначала прочитай соответствующий раздел.
При расхождении кода и спецификации — остановись и сообщи, не «исправляй» молча.

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

## Архитектура (кратко)

```
Data Layer → Signal Engine (Fusion) → Risk Gate → Execution → Interfaces
```

- **Data Layer** — котировки и история через адаптеры MT5 / CCXT / Alpaca, хранение в SQLite.
- **Signal Engine (Fusion)** — объединяет ML-предсказание и LLM-оценку в `fused_signal`; единственное место ML/LLM-логики.
- **Risk Gate** — лимиты, размер позиции, kill-switch; может только сократить или отклонить сигнал.
- **Execution** — режимы `PAPER` / `MICRO_LIVE` / `FULL_AUTO`; каждое решение пишется в `decision_audit` с версиями.
- **Interfaces** — FastAPI + WebSocket (GUI) и Typer (CLI); только читают состояние и отправляют команды.

Код: `src/augur/`. Тесты: `tests/`. CLI: `augur.interfaces.cli.main:app`.

## Команды

```bash
pip install -e ".[dev]"     # установка для разработки
pytest                      # тесты
ruff check .                # линтер
ruff format .               # форматирование
mypy src                    # типы
augur --version             # версия приложения
augur backtest              # бэктест стратегии (заглушка до соответствующего этапа)
augur kill-switch           # аварийная остановка: KILLED, ANALYSIS_ONLY, отмена ордеров, flatten
augur cb status | cb reset --confirm   # состояние CB; сброс по одноразовому коду → PAPER
augur mode show | mode set <MODE> --confirm   # режим; повышение на шаг с кодом, понижение сразу
augur orders approve <id> --confirm    # подтвердить и отправить первый ордер MICRO_LIVE
augur reconcile                        # сверка ордеров/позиций с реальной площадкой
augur news fetch | list [--symbol]      # один цикл сбора новостей (Finnhub); лента
augur news reactions | lift <id> --confirm   # активные паузы/флаги; снятие с аудитом
augur signals list | explain <id> [--json]   # лента сигналов; цепочка решения (сигнал или ордер)
augur audit explain <id> | verify-chain      # цепочка решения; проверка hash-chain audit_log
augur gui [--host 127.0.0.1] [--port 8000]   # GUI мониторинга + REST /api/v1 + /ws/events (Bearer)
```

Перед коммитом: `ruff check .` и `pytest` должны проходить.

## Skills (`.claude/skills/`)

| Skill | Когда использовать |
|---|---|
| `hybrid-ml-llm-fusion` | логика `fused_signal`, variance filter, веса ML/LLM |
| `llm-output-validation` | клиенты LLM, промпты, Pydantic-схемы ответов, запись в `llm_calls` |
| `auto-trade-risk-gate` | лимиты, размер позиции, kill-switch, проверки перед Execution |
| `release-management` | bump версии, CHANGELOG, annotated tag |
| `ml-training-inference` | признаки, разметка, walk-forward, обучение, калибровка, реестр моделей, ML-инференс (`augur.fusion.ml`) |

Если нужного skill нет в `.claude/skills/` — остановись и сообщи, не пиши код «по памяти».

## Версионирование и git

- SemVer 2.0.0. Версия меняется только вместе с записью в `CHANGELOG.md` (Keep a Changelog 1.1.0).
- Теги только annotated: `git tag -a vX.Y.Z -m "..."`.
- Новые изменения сначала пишутся в раздел `[Unreleased]`.

## Чего не делать

- Не реализовывать ML/LLM-логику вне fusion-модуля.
- Не обходить Risk Gate (ни флагами, ни «временными» ветками кода).
- Не менять версию без записи в CHANGELOG.
- Не использовать lightweight tags.
- Не логировать ключи, токены, пароли — даже частично.
- Не генерировать код без чтения спецификаций.
- Не добавлять зависимости без необходимости на текущем этапе.
- Не переходить к следующему этапу в той же сессии.