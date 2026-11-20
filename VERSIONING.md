# VERSIONING.md — Политика версионирования Janus

> Нормативный документ. Обязателен для людей и агентов (Claude Code / Codex).
> Ключевые слова «ДОЛЖЕН», «ЗАПРЕЩЕНО», «ДОПУСКАЕТСЯ» трактуются буквально.
> Агент применяет этот документ через skill `release-management` (§9).

---

## 1. Схема версионирования

### 1.1 SemVer

Формат версии приложения: `MAJOR.MINOR.PATCH` ([SemVer 2.0.0](https://semver.org/lang/ru/)).

| Часть | Растёт, когда |
|---|---|
| MAJOR | Ломающие изменения публичного контракта (только после 1.0.0) |
| MINOR | Новая функциональность; в 0.x — также ломающие изменения с пометкой **BREAKING** |
| PATCH | Только исправления ошибок |

- Стартовая версия: **0.1.0**.
- Префикс `v` — только в git-тегах (`v0.1.0`). В `pyproject.toml`, CHANGELOG, БД — без префикса.
- Пре-релизы (`0.7.0-rc.1`) допускаются только перед этапами с реальными деньгами (0.7.0, 0.11.0), в CHANGELOG отдельной секцией не выносятся.

### 1.2 Правила для 0.x.y

- Публичный API, схема БД, формат конфига и контракт Fusion **нестабильны**.
- Ломающие изменения **ДОПУСКАЮТСЯ** в MINOR (`0.4.0 → 0.5.0`) при соблюдении всех условий:
  1. Запись в CHANGELOG начинается с `**BREAKING:**`.
  2. Коммит помечен `breaking:` или `<type>!:` (§7.3).
  3. Описан путь миграции (данные, конфиг, вызывающий код).
- Ломающие изменения в PATCH **ЗАПРЕЩЕНЫ** даже в 0.x.

### 1.3 Условия перехода на 1.0.0

Все пункты обязательны:

1. Контракт Fusion `api_version` заморожен на `1.x`: в последних двух MINOR не было ломающих изменений.
2. Режим FULL_AUTO отработал ≥ 30 календарных дней без инцидентов уровня P1 (неконтролируемый ордер, нарушение лимитов Risk Gate, потеря данных).
3. Покрытие `decision_audit` — 100%: каждый ордер за период п. 2 восстанавливается до сигнала, стратегии, модели и промпта (§6).
4. Replay-тест: повторный прогон сохранённых входов через зафиксированные версии артефактов даёт тот же `risk_verdict` и параметры ордера для ≥ 99% решений; расхождения объяснены (недетерминизм LLM задокументирован).
5. Процедура отката (§8) отрепетирована на копии боевой БД минимум один раз на каждом из этапов 0.7.0 и 0.11.0.
6. Все миграции БД проходят прогон «с нуля» и «с копии боевой БД» в CI.
7. Нет открытых пометок «Устарело» без заявленной версии удаления.
8. Покрытие тестами Risk Gate и Execution ≥ 90% строк; для остального кода ≥ 75%.
9. Проведён security-review хранения ключей бирж/брокеров и прав исполнения.
10. Документы `VERSIONING.md`, `CHANGELOG.md`, спецификация Fusion и runbook отката актуальны на дату релиза.

---

## 2. Три уровня версий

### 2.1 Уровень приложения

| Носитель | Правило |
|---|---|
| `pyproject.toml` → `[project].version` | **Единственный источник истины** |
| `janus.__version__` | Только через `importlib.metadata.version("janus")`. Строковые литералы версии в коде **ЗАПРЕЩЕНЫ** |
| git tag | Annotated, `vX.Y.Z`, ставится на merge-коммит в `main` |

### 2.2 Уровень компонентов

| Версия | Формат | Семантика |
|---|---|---|
| `api_version` | `MAJOR.MINOR` (`1.0`) | Контракт Fusion. MINOR — добавлены необязательные поля. MAJOR — удаление/переименование поля, смена типа или семантики |
| `db_schema_version` | Целое, монотонно (`1, 2, 3…`) | `MAX(version)` в `schema_migrations`. Одна миграция = +1 |
| `config_version` | Целое, монотонно | Поле `config_version` в корне конфига. Растёт при любом несовместимом изменении структуры конфига |

Константы ожидаемых версий — в одном модуле:

```python
# src/janus/versions.py
FUSION_API_VERSION = "1.0"   # контракт Fusion, который реализует этот код
EXPECTED_DB_SCHEMA = 3       # схема, которую ожидает этот код
CONFIG_VERSION = 2           # формат конфига, который читает этот код
```

Поведение при старте:

- `db_schema_version < EXPECTED_DB_SCHEMA` → применить миграции (с бэкапом, §8.2).
- `db_schema_version > EXPECTED_DB_SCHEMA` → старт только если все лишние миграции имеют `compat = 'expand'` (§8.3), иначе отказ.
- `config_version` ≠ `CONFIG_VERSION` → мигратор конфига N-1 → N или отказ; конфиг новее кода → отказ.
- Входящий сигнал Fusion с другим MAJOR `api_version` → отклоняется.

```sql
CREATE TABLE schema_migrations (
    version      INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,                  -- 0003_candles_index
    checksum     TEXT    NOT NULL,                  -- sha256 файла миграции
    compat       TEXT    NOT NULL CHECK (compat IN ('expand', 'contract')),
    app_version  TEXT    NOT NULL,                  -- версия Janus, применившая миграцию
    applied_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
```

### 2.3 Уровень артефактов

| Версия | Формат | Что фиксирует |
|---|---|---|
| `strategy_version` | `<имя>_v<N>` (`strategy_v2`, `breakout_v1`) | Код стратегии + все параметры |
| `ml_model_version` | `<алгоритм>_v<N>` (`lightgbm_v3`) | Веса, набор признаков, диапазон обучающих данных, коммит кода обучения |
| `llm_prompt_version` | `prompt_<назначение>_v<N>` (`prompt_news_v1`) | Текст промпта + ID LLM-модели + параметры генерации (temperature, max_tokens) |

Правила артефактов:

- Артефакт **неизменяем**. Любое изменение (один параметр, одна строка промпта, смена LLM-модели) = новая версия `N+1`.
- Каждый артефакт регистрируется в реестре с хешем; при загрузке хеш проверяется.
- Удаление зарегистрированного артефакта **ЗАПРЕЩЕНО** (иначе теряется воспроизводимость старых ордеров).
- Значения `latest`, `current`, `dev` в качестве версии **ЗАПРЕЩЕНЫ**.
- Если артефакт в решении не участвует (например, ML до 0.10.0) — явное значение `n/a`. NULL и пустая строка запрещены.

```sql
CREATE TABLE artifacts (
    kind        TEXT NOT NULL CHECK (kind IN ('strategy', 'ml_model', 'llm_prompt')),
    version     TEXT NOT NULL,                     -- lightgbm_v3
    sha256      TEXT NOT NULL,
    path        TEXT NOT NULL,                     -- artifacts/ml/lightgbm_v3.txt
    metadata    TEXT NOT NULL,                     -- JSON: features, train_range, llm_model, temperature…
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (kind, version)
);
```

### 2.4 Сводная таблица

| Уровень | Версия | Где хранится | Кто увеличивает | Когда |
|---|---|---|---|---|
| Приложение | `app_version` | `pyproject.toml`; git tag `vX.Y.Z`; CHANGELOG | Release-владелец (агент через skill, подтверждает человек) | В release-PR `develop → main` |
| Компонент | `api_version` | `versions.py`, спецификация Fusion, поле каждого сигнала | Автор изменения контракта | В PR, меняющем контракт Fusion |
| Компонент | `db_schema_version` | Таблица `schema_migrations`; `EXPECTED_DB_SCHEMA` | Автор миграции | В PR, добавляющем файл `migrations/NNNN_*.sql` |
| Компонент | `config_version` | Корень конфига; `CONFIG_VERSION` | Автор изменения конфига | В PR с несовместимым изменением структуры конфига |
| Артефакт | `strategy_version` | Реестр `artifacts`, `artifacts/strategies/` | Автор стратегии | При любом изменении кода или параметров стратегии |
| Артефакт | `ml_model_version` | Реестр `artifacts`, `artifacts/ml/` | Пайплайн обучения (автоматически) | При каждом переобучении / смене признаков |
| Артефакт | `llm_prompt_version` | Реестр `artifacts`, `artifacts/prompts/` | Автор промпта | При изменении текста, LLM-модели или параметров генерации |

Версии компонентов и артефактов растут **независимо** от версии приложения (§5.4).

---

## 3. Формат CHANGELOG.md

### 3.1 Формат

[Keep a Changelog 1.1.0](https://keepachangelog.com/ru/1.1.0/), на русском.

- Вверху всегда секция `## [Не выпущено]`.
- Секции релизов: `## [X.Y.Z] — YYYY-MM-DD`, от новых к старым.
- Сразу под заголовком релиза — строка `**Компоненты:**` с версиями, изменившимися в релизе.
- Разделы строго в этом порядке, пустые не пишутся:

| Раздел | Содержимое |
|---|---|
| `### Добавлено` | Новая функциональность |
| `### Изменено` | Изменения существующего поведения |
| `### Устарело` | Что будет удалено и в какой версии |
| `### Удалено` | Удалённая функциональность |
| `### Исправлено` | Исправления ошибок |
| `### Безопасность` | Уязвимости, ключи, права исполнения |

- Ломающая запись начинается с `**BREAKING:**` и содержит путь миграции.
- Ссылки на сравнение версий — внизу файла.

### 3.2 Правило времени записи

- Запись в CHANGELOG делается **в том же PR**, что и изменение (в секцию `[Не выпущено]`), — до мержа в `develop`.
- Release-PR в `main` переносит `[Не выпущено]` в секцию `[X.Y.Z]`. Мерж в `main` без секции новой версии **блокируется CI** (§7.4).
- Изменения без влияния на поведение (`docs:`, `test:`, `ci:`, рефакторинг) — не записываются.
- Смена активного артефакта в боевом конфиге — запись в `Изменено`.

### 3.3 Пример полной записи для 0.1.0

```markdown
## [0.1.0] — 2026-11-27

**Компоненты:** db_schema_version 1 · config_version 1 · api_version — · артефакты —

### Добавлено
- Каркас пакета `janus` (Python 3.11+, src-layout, `pyproject.toml` как единственный источник версии).
- `janus.__version__` через `importlib.metadata`.
- Модуль `janus.versions` с ожидаемыми версиями компонентов.
- Мигратор SQLite: таблица `schema_migrations` с checksum и флагом совместимости, авто-бэкап перед миграцией.
- Миграция `0001_init`: служебные таблицы `schema_migrations`, `artifacts`.
- Загрузчик конфига с проверкой `config_version`.
- CLI-команда `janus version` — выводит версии приложения, схемы БД, конфига.
- Структурированное логирование (JSON), версия приложения в каждой записи.
- CI: линтер, типизация, тесты, проверка совпадения версии `pyproject.toml` и git-тега.
- Документы: `VERSIONING.md`, `CHANGELOG.md`, skill `release-management`.

### Безопасность
- Секреты (API-ключи бирж) читаются только из переменных окружения; `.env` в `.gitignore`.
```

---

## 4. Маппинг версий на этапы

| Версия | Этап | Готово (критерий релиза) | Компоненты / артефакты, которые растут |
|---|---|---|---|
| **0.1.0** | Каркас | Пакет, CI, мигратор, загрузчик конфига, `janus version` | `db_schema` → 1; `config` → 1 |
| **0.2.0** | Data Layer | Хранилище OHLCV/инструментов, адаптеры Forex/крипто/акции (только чтение), идемпотентная загрузка истории | `db_schema` +N (instruments, candles); `config` +1 (источники данных) |
| **0.3.0** | Стратегии + бэктест | Интерфейс стратегии, реестр, детерминированный бэктест с отчётом, Signal Engine | `db_schema` +N (signals, backtest_runs); первый `strategy_v1` |
| **0.4.0** | Fusion-контракт | Спецификация и валидация контракта, stub-реализация Fusion, таблица fused_signals | `api_version` → 1.0; `db_schema` +1 |
| **0.5.0** | Risk Gate | Лимиты (позиция, дневной убыток, экспозиция), вердикты, таблица `decision_audit` | `db_schema` +1; `config` +1 (лимиты риска) |
| **0.6.0** | PAPER | Симулятор исполнения, таблица orders, жёсткая проверка полноты версий перед ордером (§6.2) | `db_schema` +N (orders, fills); `config` +1 (режим исполнения) |
| **0.7.0** | MICRO_LIVE | Реальное исполнение с микролимитами, kill switch, сверка позиций с брокером | `config` +1 (ключи, микролимиты); `db_schema` по необходимости |
| **0.8.0** | News | Сбор и нормализация новостей, LLM-оценка новостей | `db_schema` +N (news); первый `prompt_news_v1`; `api_version` 1.1 (поле news_score) |
| **0.9.0** | GUI/CLI | Полный CLI, GUI мониторинга, просмотр цепочки решения по ордеру | `config` по необходимости |
| **0.10.0** | ML+LLM | Реальная реализация Fusion: ML-модель + LLM, реестр моделей, replay-тест | `ml_model` с `lightgbm_v1`; новые `strategy_vN`, `prompt_*_vN`; `api_version` 1.x или **BREAKING** 2.0 |
| **0.11.0** | FULL_AUTO | Автоторговля без подтверждения человека в рамках лимитов Risk Gate, алертинг | `config` +1 (режим FULL_AUTO); `db_schema` по необходимости |
| **1.0.0** | Stable | Выполнены все условия §1.3 | `api_version` заморожен на 1.x/2.x; далее строгий SemVer |

Между этапами допускаются PATCH-релизы (`0.6.1`, `0.6.2`) и промежуточные MINOR, если этап требует нескольких релизов: тогда нумерация сдвигается, а этап фиксируется в CHANGELOG.

---

## 5. Правила перехода

### 5.1 PATCH (`X.Y.Z → X.Y.Z+1`)

- Только исправления ошибок.
- **ЗАПРЕЩЕНО:** новые фичи, новые поля конфига, изменение схемы БД, изменение `api_version`.
- Если исправление требует миграции БД → это MINOR.
- Источник — ветка `hotfix/*` от `main`.

### 5.2 MINOR (`X.Y.Z → X.Y+1.0`)

- Новая функциональность с обратной совместимостью.
- Любое изменение схемы БД ДОЛЖНО идти через новую миграцию (`migrations/NNNN_*.sql`) и попадает только в MINOR или MAJOR.
- Изменение схемы без миграции (ручной `ALTER`, правка применённой миграции) **ЗАПРЕЩЕНО**.

### 5.3 BREAKING внутри 0.x

- Повышается MINOR, не MAJOR.
- Пометка `**BREAKING:**` в CHANGELOG + путь миграции.
- Миграция с `compat = 'contract'` всегда считается BREAKING.

### 5.4 Независимость версий

| Изменение | app_version | Версия компонента/артефакта |
|---|---|---|
| Исправлен баг в адаптере | PATCH | — |
| Новая таблица в БД | MINOR | `db_schema` +1 |
| Добавлено необязательное поле в контракт Fusion | MINOR | `api_version` +0.1 |
| Удалено поле из контракта Fusion | MINOR (0.x, BREAKING) / MAJOR (≥1.0) | `api_version` +1.0 |
| Переобучена модель | — (релиз не нужен) | `ml_model` +1 |
| Изменён текст промпта | — | `llm_prompt` +1 |
| Изменены параметры стратегии | — | `strategy` +1 |
| Новый тип стратегии в коде | MINOR | новый `<имя>_v1` |
| Переименован ключ конфига | MINOR (BREAKING в 0.x) | `config` +1 |

Новая версия артефакта не требует релиза приложения, но её активация в боевом конфиге фиксируется в CHANGELOG (§3.2) и автоматически — в `decision_audit`.

---

## 6. Воспроизводимость ордеров

### 6.1 Таблица `decision_audit`

```sql
CREATE TABLE decision_audit (
    id                  INTEGER PRIMARY KEY,
    signal_id           INTEGER NOT NULL REFERENCES signals(id),
    fused_signal_id     INTEGER NOT NULL REFERENCES fused_signals(id),
    app_version         TEXT NOT NULL CHECK (length(app_version) > 0
                                             AND app_version NOT LIKE '%+unknown'),
    strategy_version    TEXT NOT NULL CHECK (length(strategy_version) > 0),
    ml_model_version    TEXT NOT NULL CHECK (length(ml_model_version) > 0),
    llm_prompt_version  TEXT NOT NULL CHECK (length(llm_prompt_version) > 0),
    risk_verdict        TEXT NOT NULL CHECK (risk_verdict IN ('APPROVED', 'REDUCED', 'REJECTED')),
    execution_result    TEXT,               -- JSON; NULL до отправки, заполняется один раз
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Append-only: версии и вердикт неизменяемы, удаление запрещено
CREATE TRIGGER decision_audit_no_delete BEFORE DELETE ON decision_audit
BEGIN SELECT RAISE(ABORT, 'decision_audit is append-only'); END;

CREATE TRIGGER decision_audit_immutable BEFORE UPDATE ON decision_audit
WHEN NEW.signal_id          IS NOT OLD.signal_id
  OR NEW.fused_signal_id    IS NOT OLD.fused_signal_id
  OR NEW.app_version        IS NOT OLD.app_version
  OR NEW.strategy_version   IS NOT OLD.strategy_version
  OR NEW.ml_model_version   IS NOT OLD.ml_model_version
  OR NEW.llm_prompt_version IS NOT OLD.llm_prompt_version
  OR NEW.risk_verdict       IS NOT OLD.risk_verdict
  OR NEW.created_at         IS NOT OLD.created_at
  OR OLD.execution_result   IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'decision_audit row is immutable'); END;
```

Рекомендуемые дополнительные поля (с 0.6.0): `api_version`, `db_schema_version`, `config_version`, `git_commit`, `mode` (`PAPER` / `MICRO_LIVE` / `FULL_AUTO`).

### 6.2 Правило исполнения

**Ни один ордер не отправляется, если хотя бы одно поле версии не заполнено.**

Порядок, обязательный для Execution:

1. Сформировать запись `decision_audit` со всеми версиями и `risk_verdict`.
2. Проверить полноту (код ниже) → `INSERT` → `COMMIT`.
3. Только после успешного `COMMIT` отправить ордер.
4. Записать `execution_result` (единственный допустимый `UPDATE`).

Ошибка на шагах 1–2 → ордер не отправляется, алерт уровня P1.

```python
REQUIRED_VERSION_FIELDS = (
    "app_version", "strategy_version", "ml_model_version", "llm_prompt_version",
)
FORBIDDEN_VALUES = {"", "latest", "current", "dev", "unknown"}


class UnauditableOrderError(RuntimeError):
    pass


def assert_auditable(rec: "DecisionAuditRecord") -> None:
    bad = [
        f for f in REQUIRED_VERSION_FIELDS
        if (getattr(rec, f) or "").strip().lower() in FORBIDDEN_VALUES
    ]
    if rec.app_version.endswith("+unknown"):
        bad.append("app_version")
    if bad:
        raise UnauditableOrderError(f"missing/invalid versions: {sorted(set(bad))}")
```

### 6.3 Пример цепочки

```text
Janus 0.10.0                                  app_version
 └─ strategy_v2         → signal #8812        strategy_version
     ├─ lightgbm_v3                           ml_model_version
     └─ prompt_news_v1                        llm_prompt_version
         → fused_signal #4410 (api_version 1.1)
             → Risk Gate: REDUCED (0.5 → 0.3 лота)
                 → decision_audit #1907
                     → order → FILLED @ 1.08412
```

Восстановление постфактум:

```sql
SELECT d.id, d.created_at, d.app_version,
       d.strategy_version, d.ml_model_version, d.llm_prompt_version,
       d.risk_verdict, d.execution_result,
       s.payload  AS signal_payload,
       f.payload  AS fused_payload,
       am.sha256  AS model_sha256,
       ap.sha256  AS prompt_sha256,
       ap.metadata AS prompt_meta          -- LLM-модель, temperature
FROM decision_audit d
JOIN signals       s  ON s.id = d.signal_id
JOIN fused_signals f  ON f.id = d.fused_signal_id
LEFT JOIN artifacts am ON am.kind = 'ml_model'   AND am.version = d.ml_model_version
LEFT JOIN artifacts ap ON ap.kind = 'llm_prompt' AND ap.version = d.llm_prompt_version
WHERE d.id = 1907;
```

Код приложения на момент решения: `git checkout v0.10.0`.

---

## 7. Git-политика

### 7.1 Ветки

| Ветка | От | Мерж в | Назначение |
|---|---|---|---|
| `main` | — | — | Только релизы. Каждый коммит = тег `vX.Y.Z`. Прямые пуши запрещены |
| `develop` | `main` | `main` (release-PR) | Интеграция. Секция `[Не выпущено]` в CHANGELOG |
| `feature/<scope>-<кратко>` | `develop` | `develop` | Фичи, MINOR-изменения |
| `hotfix/<X.Y.Z>-<кратко>` | `main` | `main` + `develop` | Только PATCH |

Релизный цикл:

1. Release-PR `develop → main`: bump версии в `pyproject.toml`, перенос `[Не выпущено]` → `[X.Y.Z]`, коммит `chore(release): X.Y.Z`.
2. После мержа — annotated tag на merge-коммит в `main`.
3. Мерж `main` обратно в `develop`.

### 7.2 Теги

- Только annotated: `git tag -a vX.Y.Z -m "..."`. Подпись (`-s`) рекомендуется.
- Формат строго `vX.Y.Z` (или `vX.Y.Z-rc.N`).
- Перемещение (`git tag -f`) и удаление опубликованного тега **ЗАПРЕЩЕНЫ**. Ошибочный релиз исправляется новой PATCH-версией.
- Тело тега: краткое содержание релиза + версии компонентов (пример — §10.3).

### 7.3 Коммиты — Conventional Commits

```text
<type>(<scope>): <описание в повелительном наклонении>

[тело]

[BREAKING CHANGE: <что сломано и как мигрировать>]
```

| Type | Назначение | Влияние на версию |
|---|---|---|
| `feat` | Новая функциональность | MINOR |
| `fix` | Исправление ошибки | PATCH |
| `breaking` | Ломающее изменение (эквивалент `<type>!:` или футера `BREAKING CHANGE:`) | MINOR в 0.x / MAJOR ≥ 1.0 |
| `chore` | Сборка, релиз, зависимости | — |
| `refactor`, `perf`, `test`, `docs`, `ci` | Без изменения поведения | — |

Scope: `data`, `adapters`, `strategy`, `backtest`, `signal`, `fusion`, `risk`, `exec`, `news`, `ui`, `db`, `config`, `release`.

Примеры:

```text
feat(adapters): добавить адаптер Binance spot (только чтение)
fix(risk): учитывать комиссию в дневном лимите убытка
breaking(fusion): удалить поле confidence_raw из контракта, api_version 2.0
feat(db)!: разделить таблицу orders на orders и fills
chore(release): 0.6.0
```

### 7.4 CI-проверки

| Событие | Проверка |
|---|---|
| Любой PR | В `src/` нет литералов `__version__ = "..."`; применённые миграции не изменены (checksum) |
| PR в `develop` с `feat`/`fix`/`breaking` | `CHANGELOG.md` изменён |
| PR в `main` | Есть секция `## [X.Y.Z]`, совпадающая с `pyproject.toml`; тип PR — release или hotfix |
| Push тега `v*` | Тег annotated; `pyproject.toml` == тег; секция в CHANGELOG существует |

```yaml
# .github/workflows/release-check.yml
name: release-check
on:
  push:
    tags: ["v*.*.*"]
jobs:
  version-match:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - run: git fetch --force --tags     # сохранить объект annotated-тега
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Тег annotated
        run: test "$(git cat-file -t "refs/tags/$GITHUB_REF_NAME")" = "tag"
      - name: pyproject.toml == tag
        run: python scripts/check_version.py "$GITHUB_REF_NAME"
      - name: Секция в CHANGELOG
        run: grep -qE "^## \[${GITHUB_REF_NAME#v}\] — [0-9]{4}-[0-9]{2}-[0-9]{2}$" CHANGELOG.md
```

```python
# scripts/check_version.py
import pathlib
import sys
import tomllib

tag = sys.argv[1].removeprefix("refs/tags/")
if not tag.startswith("v"):
    sys.exit(f"Тег без префикса v: {tag}")

version = tomllib.loads(
    pathlib.Path("pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]

if tag[1:] != version:
    sys.exit(f"Расхождение: pyproject.toml={version}, tag={tag}")
print(f"OK: {version}")
```

---

## 8. Роллинг и откат

### 8.1 Политика миграций: forward-only + expand/contract

- Down-миграции **не пишутся**. Причины: ограничения `ALTER TABLE` в SQLite, риск потери торговых данных, down-миграции на практике не тестируются.
- Каждая миграция объявляет совместимость в заголовке и в `schema_migrations.compat`:
  - `expand` — только добавление (таблица, nullable-колонка, индекс). Код версии N-1 работает со схемой N.
  - `contract` — удаление/переименование/смена типа. Всегда **BREAKING**.
- Изменение «переименовать колонку» делается в два MINOR-релиза:
  1. `expand`: добавить новую колонку, код пишет в обе, читает новую.
  2. `contract` (следующий MINOR): удалить старую.
- Окно безопасного отката = один MINOR без `contract`-миграций.
- Применённая миграция неизменна; checksum проверяется при старте.

```sql
-- migrations/0004_orders_client_id.sql
-- compat: expand
ALTER TABLE orders ADD COLUMN client_order_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS ix_orders_client_order_id ON orders(client_order_id);
```

### 8.2 Обязательный бэкап

Мигратор ДОЛЖЕН перед каждой миграцией выполнить:

```bash
sqlite3 data/janus.db "VACUUM INTO 'backups/janus_pre_v0.6.0_schema5.db'"
```

Бэкапы перед миграциями хранятся не меньше двух MINOR-релизов.

### 8.3 Процедура отката на предыдущую версию

| Шаг | Действие | Команда / проверка |
|---|---|---|
| 1 | Остановить новые ордера | `janus exec safe-mode on` (kill switch). Открытые позиции — по runbook, не автоматически |
| 2 | Снять бэкап текущей БД | `sqlite3 data/janus.db "VACUUM INTO 'backups/janus_pre_rollback_$(date +%Y%m%dT%H%M%S).db'"` |
| 3 | Откатить код | `git checkout vX.Y.Z` + установка из lock-файла с хешами (`uv sync --frozen` / `pip install --require-hashes -r requirements.lock`) |
| 4а | Схема: только `expand`-миграции после vX.Y.Z | Ничего не делать: старый код стартует на новой схеме |
| 4б | Схема: есть `contract`-миграция | Восстановить `backups/janus_pre_vX.Y.Z_*.db`; экспортировать из текущей БД строки append-only таблиц (`orders`, `fills`, `decision_audit`) после момента бэкапа и импортировать скриптом `scripts/reimport_since.py`. Потеря торговых записей недопустима |
| 5 | Конфиг | Конфиг из тега vX.Y.Z; `config_version` должен совпасть с `CONFIG_VERSION` кода |
| 6 | Артефакты | Переключить активные указатели в конфиге на прежние версии (`active_model: lightgbm_v2`). Файлы артефактов не удаляются |
| 7 | Проверка | `janus doctor`: версии приложения, схемы, конфига, хеши активных артефактов — всё зелёное |
| 8 | Возврат в торговлю | PAPER ≥ 1 торговой сессии → снять safe-mode |

- Откат не переписывает теги и историю. Исправление выпускается новой PATCH/MINOR-версией.
- Откат только артефакта (модель/промпт) не требует шагов 2–5: переключить указатель → `janus doctor` → PAPER-прогон.

---

## 9. Skill для Claude Code: `release-management`

- **Путь:** `.claude/skills/release-management/SKILL.md`
- **Назначение:** единая процедура выбора версии, записи CHANGELOG, миграций, тегов и регистрации артефактов.
- **Триггер:** любой коммит/PR в `main`; изменение версии приложения, схемы БД, конфига, контракта Fusion; создание или активация артефакта; создание git-тега.

````markdown
---
name: release-management
description: Версионирование и релизы Janus. Использовать перед любым коммитом или PR в main, при смене версии приложения, при изменении схемы БД, формата конфига, контракта Fusion, при создании или активации ML-модели, стратегии или LLM-промпта, при создании git-тега и записи в CHANGELOG.md.
---

# Release management — Janus

Источник правил: `VERSIONING.md` в корне репозитория. При конфликте побеждает VERSIONING.md.

## Жёсткие запреты
- Не писать версию литералом в коде. Только `pyproject.toml` + `importlib.metadata`.
- Не создавать lightweight-теги. Не перемещать и не удалять опубликованные теги.
- Не изменять применённые миграции. Не писать down-миграции.
- Не перезаписывать файлы артефактов. Не использовать `latest`/`current` как версию.
- Не мержить в main и не пушить теги без явного подтверждения человека.

## Процедура для каждого изменения (PR в develop)
1. Классифицируй изменение по таблице:
   | Изменение | Действие |
   |---|---|
   | Багфикс без схемы БД | `fix:`, запись в «Исправлено» |
   | Новая фича | `feat:`, запись в «Добавлено» |
   | Изменение схемы БД | новый файл `migrations/NNNN_<имя>.sql` с `-- compat: expand|contract`, `EXPECTED_DB_SCHEMA += 1` |
   | `compat: contract`, удаление поля контракта, переименование ключа конфига | `breaking:` / `!`, запись с `**BREAKING:**` + путь миграции |
   | Контракт Fusion | обновить `FUSION_API_VERSION` и спецификацию |
   | Формат конфига | `CONFIG_VERSION += 1`, мигратор конфига N-1 → N |
   | Новая версия артефакта | новый файл `<имя>_v<N+1>`, регистрация в `artifacts` с sha256 |
2. Добавь запись в `## [Не выпущено]` CHANGELOG.md в том же коммите/PR.
3. Запусти `pytest`, проверку миграций с нуля и на копии БД.

## Процедура релиза (develop → main)
1. Определи версию: есть BREAKING или `feat` → MINOR; только `fix` → PATCH (только из hotfix/*).
2. Обнови `[project].version` в `pyproject.toml`.
3. Перенеси `[Не выпущено]` → `## [X.Y.Z] — YYYY-MM-DD`, добавь строку `**Компоненты:**`, обнови ссылки сравнения внизу.
4. Коммит: `chore(release): X.Y.Z`. Открой PR в main. Жди подтверждения человека.
5. После мержа: `git tag -a vX.Y.Z -m "<тело по шаблону>"` на merge-коммит, `git push origin vX.Y.Z`.
6. Смержи main обратно в develop.

## Проверки перед завершением
```bash
python -c "import janus, tomllib; v=tomllib.load(open('pyproject.toml','rb'))['project']['version']; assert janus.__version__==v, (janus.__version__, v)"
grep -rnE '__version__\s*=\s*"' src/ && exit 1 || true
git cat-file -t vX.Y.Z   # должно вывести: tag
janus doctor
```

## Пример
Задача: «Добавь в orders колонку client_order_id».
- `migrations/0004_orders_client_id.sql`, `-- compat: expand`
- `EXPECTED_DB_SCHEMA = 4`
- Коммит: `feat(db): добавить client_order_id в orders`
- CHANGELOG `[Не выпущено]` → «Добавлено»: «Колонка `orders.client_order_id` для идемпотентной отправки ордеров (db_schema_version 4).»

## Anti-patterns
- Версия в `pyproject.toml`, `__init__.py` и теге расходится → единственный источник — pyproject.toml.
- Lightweight-тег `git tag v0.3.0` → только `git tag -a`.
- «CHANGELOG допишу потом» → запись в том же PR, иначе CI блокирует.
- Модель `model.pkl` / `lightgbm_latest` без версии и хеша → `lightgbm_v<N>` + регистрация.
- Правка промпта на месте → новый `prompt_*_v<N+1>`.
- Миграция схемы в hotfix → это MINOR.
````

---

## 10. Примеры

### 10.1 CHANGELOG.md (фрагмент: шапка, 0.2.0, ссылки)

Полная запись 0.1.0 — в §3.3.

```markdown
# Журнал изменений

Все значимые изменения проекта Janus. Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версионирование — [SemVer](https://semver.org/lang/ru/), правила — `VERSIONING.md`.

## [Не выпущено]

### Добавлено
- Интерфейс `Strategy` и реестр стратегий.

## [0.2.0] — 2026-12-11

**Компоненты:** db_schema_version 1 → 3 · config_version 1 → 2

### Добавлено
- Таблицы `instruments` и `candles` (миграции `0002_instruments`, `0003_candles`, compat: expand).
- Адаптеры только для чтения: OANDA (Forex), Binance spot (крипто), Alpaca (акции).
- Идемпотентная загрузка истории OHLCV с докачкой пропусков.
- Нормализация таймфреймов и часовых поясов (хранение в UTC).
- CLI: `janus data fetch`, `janus data gaps`.

### Изменено
- **BREAKING:** секция конфига `exchanges` переименована в `sources` (config_version 2).
  Миграция: `janus config migrate` либо ручное переименование ключа.

### Исправлено
- `janus version` падал при отсутствии файла БД.

### Безопасность
- Ключи адаптеров маскируются в логах.

[Не выпущено]: https://github.com/<org>/janus/compare/v0.2.0...develop
[0.2.0]: https://github.com/<org>/janus/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/<org>/janus/releases/tag/v0.1.0
```

### 10.2 pyproject.toml с версией из одного источника

```toml
[build-system]
requires = ["hatchling>=1.24"]
build-backend = "hatchling.build"

[project]
name = "janus"
version = "0.1.0"                 # ЕДИНСТВЕННОЕ место, где записана версия приложения
description = "Гибридная торговая система ML+LLM"
requires-python = ">=3.11"
dependencies = []

[project.scripts]
janus = "janus.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/janus"]
```

```python
# src/janus/__init__.py
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("janus")
except PackageNotFoundError:          # запуск из исходников без `pip install -e .`
    __version__ = "0.0.0+unknown"     # Execution отклоняет ордера с такой версией (§6.2)
```

### 10.3 Annotated git tag

```bash
git checkout main && git pull --ff-only

git tag -a v0.2.0 -m "Janus 0.2.0 — Data Layer

Добавлено: хранилище OHLCV, адаптеры OANDA/Binance/Alpaca (read-only),
идемпотентная загрузка истории.
BREAKING: config.exchanges -> config.sources.

Компоненты: db_schema_version 3, config_version 2
Артефакты: -"

git push origin v0.2.0

git cat-file -t v0.2.0      # -> tag
git show v0.2.0 --no-patch  # тело аннотации, автор, дата
```

---

## Анти-паттерны версионирования

1. **Версия в трёх местах расходится.** `pyproject.toml` = 0.4.0, `__init__.py` = "0.3.2", тег v0.4.1. Источник один — `pyproject.toml`.
2. **Lightweight-теги.** Нет автора, даты и описания; CI их отклоняет.
3. **«CHANGELOG потом».** Запись делается в том же PR, иначе релиз собирается по памяти из `git log`.
4. **Модели без версий.** `model.pkl`, `lightgbm_latest.txt`, перезапись файла при переобучении — старые ордера становятся необъяснимыми.
5. **Правка промпта или параметров стратегии на месте.** Одна и та же `prompt_news_v1` выдаёт разные решения в разные дни.
6. **Смена LLM-модели без новой `llm_prompt_version`.** Текст промпта тот же, поведение — другое.
7. **Изменение применённой миграции** или ручной `ALTER TABLE` на боевой БД.
8. **Миграция схемы в PATCH/hotfix.** Ломает гарантию «PATCH безопасно накатывать и откатывать».
9. **Перемещение или удаление опубликованного тега** (`git tag -f`, `git push --delete`). Ошибочный релиз исправляется новой версией.
10. **`latest`/`current`/NULL в `decision_audit`** или отправка ордера до `COMMIT` записи аудита.

---

## Чек-лист перед релизом

- [ ] Ветка релиза — `develop` (MINOR) или `hotfix/*` (PATCH); тип изменений соответствует версии (§5).
- [ ] `[project].version` в `pyproject.toml` обновлена; литералов версии в `src/` нет.
- [ ] `janus.__version__` после установки совпадает с `pyproject.toml`.
- [ ] Секция `[Не выпущено]` перенесена в `## [X.Y.Z] — YYYY-MM-DD`; ссылки сравнения обновлены.
- [ ] Строка `**Компоненты:**` отражает все изменившиеся версии.
- [ ] Каждое ломающее изменение помечено `**BREAKING:**` и содержит путь миграции.
- [ ] Новые миграции имеют `-- compat: expand|contract`; `EXPECTED_DB_SCHEMA` обновлён; checksum применённых миграций не изменились.
- [ ] Миграции проходят с нуля и на копии боевой БД; бэкап перед миграцией создаётся автоматически.
- [ ] `CONFIG_VERSION` и мигратор конфига обновлены при изменении формата конфига.
- [ ] `FUSION_API_VERSION` и спецификация Fusion согласованы.
- [ ] Все активные артефакты в конфиге зарегистрированы в `artifacts`, хеши совпадают с файлами.
- [ ] Тест «ордер без полной версии не отправляется» зелёный; replay-тест на эталонных решениях зелёный.
- [ ] `janus doctor` на стенде PAPER без ошибок.
- [ ] Процедура отката для этого релиза определена: `expand`-only или требуется восстановление бэкапа (§8.3).
- [ ] Человек подтвердил мерж в `main`; после мержа создан annotated-тег `vX.Y.Z`, CI `release-check` зелёный, `main` смержен в `develop`.
