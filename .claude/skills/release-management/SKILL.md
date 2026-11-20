---
name: release-management
description: Release discipline for Janus - SemVer bump, CHANGELOG.md ([Unreleased] → version, BREAKING marks), single version source in pyproject.toml, pytest + ruff gates, annotated git tags, DB migrations with db_schema_version, model/strategy/prompt versioning and rollback. Use before committing to main, before changing the app version, DB schema or the fused_signal contract, and before creating a git tag or rolling back a release. Do NOT use for writing trading, fusion or LLM logic itself (use auto-trade-risk-gate, hybrid-ml-llm-fusion, llm-output-validation).
---

# release-management

SemVer, CHANGELOG, теги, миграции и rollback. Цель: любой ордер воспроизводим по версиям из `decision_audit`.

## When to use
- Коммитишь или мержишь в `main`.
- Меняешь версию приложения, схему БД (`db_schema_version`) или контракт `fused_signal`.
- Создаёшь git tag или откатываешь релиз.

When NOT to use:
- Обычная работа в feature-ветке без изменения версий или схемы.
- Логика торговли, Fusion, LLM → соответствующие skills.

## Procedure
1. Прочитай `VERSIONING.md`: три уровня версий, `decision_audit`, git-политика, rollback.
2. Определи тип изменения по SemVer:
   - MAJOR: ломающее изменение контракта `fused_signal`, несовместимая схема БД, CLI или конфиг;
   - MINOR: новая функциональность с обратной совместимостью;
   - PATCH: исправления.
3. Обнови `CHANGELOG.md`: перенеси `[Unreleased]` в `[X.Y.Z] - YYYY-MM-DD` с разделами «Добавлено», «Изменено», «Удалено», «Исправлено», «Безопасность». Ломающие пункты помечай **BREAKING** с инструкцией миграции. Затем создай пустой `[Unreleased]`.
4. Обнови версию только в `pyproject.toml` (`[project].version`). В коде версия читается через `importlib.metadata.version("janus")`.
5. Схема БД изменилась → новая миграция `migrations/NNNN_<name>.sql` (forward и, если возможно, down), `db_schema_version` +1. Миграцию прогоняй на копии рабочей SQLite.
6. Проверь версии артефактов: новые или изменённые ML-модели (`ml_models.version`), стратегии (`strategy_version`) и промпты (`llm_prompt_version`) имеют явную версию и запись в CHANGELOG.
7. Прогони гейты. Любой красный результат блокирует релиз:
   - `pytest`
   - `ruff check .`
8. Закоммить: `chore(release): vX.Y.Z`.
9. Создай annotated tag: `git tag -a vX.Y.Z -m "Janus vX.Y.Z: <кратко>"`.
10. Сверь версии: pyproject, тег и заголовок CHANGELOG должны совпадать (скрипт ниже).
11. Rollback: `git checkout vPREV`, down-миграция или восстановление бэкапа БД, сделанного до миграции, затем перезапуск в PAPER. Зафиксируй откат в CHANGELOG (раздел «Исправлено») и в `audit_log`.

## Examples
```markdown
## [Unreleased]

## [1.4.0] - 2026-11-20
### Добавлено
- Variance filter для LLM-сэмплов (`llm_prompt_version` 3).
### Изменено
- **BREAKING:** `fused_signal.risk_flags` теперь `list[str]`; миграция 0012.
### Безопасность
- Kill switch переживает рестарт.
```

```bash
pytest && ruff check .
git tag -a v1.4.0 -m "Janus v1.4.0: variance filter, persistent kill switch"
```

```bash
# проверка совпадения версий
V=$(python -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
T=$(git describe --tags --exact-match | sed 's/^v//')
grep -q "^## \[$V\]" CHANGELOG.md && [ "$V" = "$T" ] && echo OK || echo "MISMATCH $V/$T"
```

## Anti-patterns
- **Lightweight tags вместо annotated.** Нет автора, даты и сообщения, а `git describe` и аудит релизов работают ненадёжно.
- **Версия в трёх местах расходится.** `decision_audit.app_version` перестаёт указывать на реальный код, и ордер нельзя воспроизвести.
- **CHANGELOG пишется «потом».** Детали забываются, ломающие изменения проходят без предупреждения.
- **Модели или стратегии без версий.** Два разных поведения выглядят в аудите одинаково, и разбор инцидента невозможен.
- **Ломающее изменение без пометки BREAKING.** Потребители `fused_signal` и миграции падают в рантайме, а не при обновлении.
- **Изменение схемы без миграции и без +1 к `db_schema_version`.** Старая и новая версии приложения пишут в несовместимую БД, и rollback ломает данные.