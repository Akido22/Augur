# Changelog

Все значимые изменения проекта Augur документируются в этом файле.

Формат основан на [Keep a Changelog 1.1.0](https://keepachangelog.com/ru/1.1.0/),
проект придерживается [Semantic Versioning 2.0.0](https://semver.org/lang/ru/).

## [Unreleased]

**Компоненты (готовится 0.8.0 — News):** db_schema_version 5 → 6 · api_version 1.0 (без изменений) · config_version 4 → 5 · метод скоринга `rule:v1` (без LLM, `llm_prompt_version` не появляется)

Блок 0.8.0 идёт отдельно от 0.7.0: 0.7.0 ещё не выпущен. При релизе 0.7.0 его блок ниже станет
`[0.7.0]`, а этот останется в `[Unreleased]`.

### Добавлено — 0.8.0

- Новостной контур `augur.news` (ARCHITECTURE.md §8, §14 этап 8): fetch → normalize → dedup
  (`content_hash` + url) → привязка к символам → `RuleBasedScorer` → `news` → `news_event` →
  `NewsReactionRules`. Каждая новость — одна транзакция (строка, реакции, аудит).
- `FinnhubFetcher`: market news по категориям (`NEWS__FINNHUB_CATEGORIES`, по умолчанию
  `crypto`). Ключ `FINNHUB_API_KEY` передаётся только заголовком `X-Finnhub-Token`, не в URL.
  Ошибки: 429/5xx/таймаут — retryable, 401/403 — нет. Протокол `NewsFetcher` готов для NewsAPI и
  Alpha Vantage.
- Нормализация: NFKC, вырезание управляющих символов, обрезка заголовка (300) и summary
  (`NEWS__SUMMARY_MAX_CHARS`). Привязка к `settings.symbols`: базовый актив или символ в
  `related`, либо базовый актив / алиас (`NEWS__SYMBOL_ALIASES`) как целое слово в тексте.
- `RuleBasedScorer` (`rule:v1`): детерминированный словарь терминов с весами, вес источника
  (`NEWS__SOURCE_WEIGHTS`); sentiment ∈ [-1, 1], confidence, importance (low/medium/high).
- `news_event/1.0` (ARCH §8.3) как Pydantic-модель `NewsEvent`; подписчики через
  `NewsPipeline.subscribe` (вызываются после COMMIT, ошибка подписчика не роняет цикл).
- `NewsReactionRules` (ARCH §8.4): `pause_symbol`, `pause_strategy`, `add_risk_flag`, `notify`.
  Условия: `sentiment_lt/gt`, `abs_sentiment_lt`, `confidence_gte`, `importance_in`,
  `window_count_gte` за `window_min`. Срок реакции — `published_ts + duration_min`. Правила по
  умолчанию — `strong_negative_pause`, `mixed_reduce_risk` (`news_mixed`), `any_high_alert`.
  Аудит: `NEWS_REACTION`, `NEWS_REACTION_LIFTED`, `NEWS_FETCH_FAILED`.
- Миграция `0006_news`: `news` (DDL ARCH §3.2 + `confidence`, `importance`, уникальный url) и
  `news_reactions` (активные эффекты со сроком, снятие человеком).
- CLI: `augur news fetch`, `augur news list [--symbol] [--limit]`, `augur news reactions`,
  `augur news lift <id> --confirm`.
- Тесты: нормализация, хэш, привязка, скорер, Finnhub на мок-транспорте httpx, пайплайн (дедуп,
  курсор, ошибки источника), реакции, R15/R16/soft-sizing из БД, миграция, CLI.

### Изменено — 0.8.0

- `RiskContextBuilder` читает активные `news_reactions`: паузы → `news_paused_symbols` /
  `news_paused_strategies` (R16; пауза по имени стратегии покрывает все версии), флаги символа →
  новое поле `RiskContext.news_flags`.
- `RiskGate`: R15 и soft-sizing учитывают `sig.risk_flags` ∪ `ctx.news_flags`. Флаги по-прежнему
  только ужесточают, CLOSE/REDUCE не блокируются.
- Конфиг (config_version 5): `NewsSettings` (`NEWS__*`), правила — `NEWS__RULES` (JSON). У всех
  полей есть дефолты.
- VERSIONING.md §4 и ARCHITECTURE.md §14: этапы пронумерованы одинаково (8 News, 9 GUI/CLI,
  10 ML+LLM, 11 FULL_AUTO). LLM-оценка новостей, `prompt_news_v1` и `api_version` 1.1 перенесены
  в 0.10.0. ARCHITECTURE.md §3.2, §8, §9.2 — по факту реализации.

### Примечания — 0.8.0

- Решения пользователя: без LLM (LLM-скорер — реализация `SentimentScorer` в fusion-модуле,
  0.10.0); только Finnhub.
- Не реализовано: `reduce_risk(multiplier)` (уменьшение — через `add_risk_flag` с кодом из
  `risk.soft_flags`), `macro_event_blackout` (нужен экономический календарь), периодический
  `poll_news` (нет оркестратора, запуск — `augur news fetch`), уведомления (`notify` пишет только
  аудит и лог), `config/news_rules.yaml` (правила в `Settings`, без новой зависимости).
- `[ДОПУЩЕНИЕ]` Словарь `rule:v1` и пороги правил — стартовые; подобрать на истории новостей.
- Проверено на копии рабочей БД: миграция 3 → 6 проходит.

**Компоненты (готовится 0.7.0 — MICRO_LIVE):** db_schema_version 5 (без изменений) · api_version 1.0 (без изменений) · config_version 3 → 4

### Добавлено

- MICRO_LIVE (ARCHITECTURE.md §1.4, §7.1, §14 этап 7): реальное исполнение на CCXT spot с
  micro-лимитами. Путь к площадке прежний: `RiskGate` → `decision_audit` (COMMIT) → `OrderManager`.
- `ModeController`: повышение режима строго на один шаг, только с одноразовым 6-значным кодом
  для MICRO_LIVE / FULL_AUTO (TTL `mode.code_ttl_s`). Условия: цель ≤ `trading_mode` и
  ≤ `max_allowed_mode`, CB в ARMED. Перед подтверждением условия проверяются повторно.
  `MODE_CHANGE_REQUESTED` → `MODE_CHANGED` (код в аудит не пишется). Понижение — без кода.
  `sync_with_config` при старте понижает режим до конфига с записью в аудит. Promotion в FULL_AUTO:
  `mode.min_micro_trades` исполненных ордеров MICRO_LIVE без срабатывания CB.
- Ручное подтверждение первого ордера MICRO_LIVE (`augur.execution.approval`). Ордер остаётся
  PENDING с `ORDER_APPROVAL_REQUIRED`. `augur orders approve <id> --confirm` проверяет код, пишет
  `ORDER_APPROVED` и `micro_first_order_confirmed=1` и отправляет ордер. Если ждать дольше TTL
  сигнала, ордер становится REJECTED. Каждый новый вход в режим снова требует подтверждения.
  Системные выходы (SL/TP/flatten) подтверждения не ждут.
- `CcxtAdapter`: `place_order` (одна попытка, `clientOrderId`, `amount_to_precision`), `get_order`
  по `clientOrderId`, идемпотентный `cancel_order`, маппинг статусов и комиссии. Флаги
  `ccxt_sandbox` (по умолчанию true — testnet) и `ccxt_quote_currency`.
- `build_live_stack` / `LiveStack`: сборка MICRO_LIVE над реальным адаптером, сверка только
  `settings.symbols`. Если текущий режим ниже стека, одобренные вердикты не исполняются:
  `EXECUTION_SKIPPED`.
- `Reconciler`: не трогает ордера, ждущие подтверждения; отклоняет просроченные. Для live-площадки
  допуск max(`qty_step`, `min_qty`), фильтр символов. Новый `check_positions()` — проверка без
  побочных эффектов (preflight `mode set`).
- CLI: `augur mode show`, `augur mode set <MODE> --confirm` (вход в MICRO_LIVE начинается со
  сверки позиций субсчёта), `augur orders approve <id> --confirm`, `augur reconcile`.
  `augur kill-switch` при конфиге MICRO_LIVE отменяет ордера и закрывает позиции на реальной
  площадке. Если площадка недоступна, KILLED всё равно выставляется, с предупреждением.
- `docs/RUNBOOK_MICRO_LIVE.md`: прогон на testnet и на реальном счёте, откат.
- Тесты: `ModeController`, e2e MICRO_LIVE (подтверждение, TTL, micro-cap, повторный вход,
  kill switch, SL без подтверждения, сверка spot), ордера CCXT на мок-бирже, CLI.

### Изменено

- **Без строки `mode_state` режим не выше PAPER**, даже если `TRADING_MODE=MICRO_LIVE`
  (`ModeStore.current`, `RiskContextBuilder.current_mode`). Конфиг только разрешает режим,
  включает его `augur mode set`.
- `CcxtAdapter.get_account_state`: equity spot-счёта = валюта котировки + базовые активы по
  последней цене. Раньше купленный актив выпадал из equity, и первая покупка выглядела бы
  дневным убытком на весь notional (R10 / CB `daily_loss`).
- `OrderManager`: принимает MICRO_LIVE только с реальным адаптером (не `paper`); PAPER — только с
  `paper`; FULL_AUTO отклоняется (0.11.0).
- `TradingPipeline` принимает `conn` первым аргументом.
- Конфиг (config_version 4): `ModeSettings` (`MODE__CODE_TTL_S`, `MODE__MIN_MICRO_TRADES`),
  `CCXT_SANDBOX`, `CCXT_QUOTE_CURRENCY`. У всех полей есть дефолты.
- ARCHITECTURE.md: §3.2 — события `ORDER_APPROVAL_REQUIRED`, `ORDER_APPROVED`,
  `EXECUTION_SKIPPED`; §4.4 и §7.1 — решения по spot и MICRO_LIVE. CLAUDE.md — новые команды.

### Безопасность

- Реальные деньги включаются только тремя явными шагами: `TRADING_MODE` / `MAX_ALLOWED_MODE`
  ≥ MICRO_LIVE, `CCXT_SANDBOX=false`, `augur mode set MICRO_LIVE --confirm` с кодом. Первый ордер
  дополнительно требует `orders approve` с кодом.
- `place_order` на CCXT никогда не повторяется вслепую (ARCH §4.2).

### Примечания

- Решения пользователя: только CCXT spot (Alpaca/MT5 — этап 10); сверка — выделенный субсчёт
  без базовых активов, без baseline-таблицы; `orders approve` отправляет ордер сам, потому что
  оркестратора пока нет.
- `[ДОПУЩЕНИЕ]` «Сделка» для promotion — FILLED-ордер MICRO_LIVE с момента входа в режим.
- `ccxt_venue_quirks.py` (ARCH §4.4) не реализован: используется унифицированный
  `clientOrderId`, проверить на testnet конкретной биржи.
- Не сделано: прогон на реальном счёте (выполняет человек по runbook) и репетиция отката на
  копии боевой БД (VERSIONING §1.3, п. 5) — оба нужны до релиза 0.7.0.

## [0.6.0] - 2026-11-27

**Компоненты:** db_schema_version 4 → 5 · api_version 1.0 (без изменений) · config_version 2 → 3 · артефакты `sma_cross@1.0.0`, `rsi@1.0.0`; `ml_model_version` / `llm_prompt_version` = `n/a` (VERSIONING.md §2.3); системные выходы — `strategy_version` `system:sl` / `system:tp` / `system:flatten`

### Добавлено

- Execution в режиме PAPER (`augur.execution`, ARCHITECTURE.md §7, §14 этап 6). Единственный путь
  к площадке: `RiskGate` → `decision_audit` (COMMIT) → `OrderManager`.
- `OrderManager`: перед ордером перечитывает закоммиченную запись `decision_audit` и проверяет
  полноту версий (`assert_auditable`, VERSIONING.md §6.2). Затем одной транзакцией создаёт PENDING
  и заполняет `decision_audit.order_id`, после этого `place_order`. Дальше по ответу площадки:
  переходы state machine → fills → позиции → `decision_audit.execution_result` (записывается один раз).
  Работает только при `mode=PAPER` и venue `paper`.
- Идемпотентность: детерминированный `client_order_id` (`jn` + base32(sha256), 24 символа,
  ARCH §7.3). Повтор того же сигнала или решения даёт `ORDER_DUPLICATE_SUPPRESSED`, на площадку
  ничего не уходит.
- Сетевые сбои (ARCH §4.2): `place_order` не повторяется вслепую. Сначала `get_order(coid)`.
  Если площадка ответила «нет такого», ордер повторяется один раз с тем же `client_order_id`.
  Если статус установить нельзя, ордер получает `UNKNOWN` (R5 блокирует символ до реконсиляции).
- State machine ордера (`ALLOWED_TRANSITIONS`, ARCH §7.3). Если площадка отвечает итоговым
  статусом, промежуточные переходы (`PENDING → SUBMITTED → FILLED`) проходятся явно, и каждый
  попадает в аудит. Запоздалые ответы по терминальным ордерам игнорируются.
- Частичные исполнения: fills дедуплицируются по `(order_id, venue_fill_id)`, позиция
  обновляется по каждому fill. Остаток PARTIAL MARKET-ордера отменяется через
  `partial_timeout_s` (60 с).
- `PositionManager` (средняя цена, частичное закрытие, переворот, realized PnL за вычетом
  комиссий) и `PortfolioTracker` (`portfolio_snapshots`: `day_pnl` от начала UTC-дня, пик,
  просадка).
- `CircuitBreaker` (ARCH §7.4). Триггеры: серия убытков, просадка, дневной убыток, ошибки
  адаптера (окно / fatal), невалидные fused_signal (серия или доля в окне), расхождение
  реконсиляции. При срабатывании: `TRIPPED` (персистентно), отмена ордеров на вход, PAPER-hold,
  `CB_TRIPPED`. Сброс — только одноразовым 6-значным кодом (TTL 120 с) → режим PAPER, `CB_RESET`.
- `KillSwitch`: `KILLED` (переживает рестарт), режим ANALYSIS_ONLY, `cancel_all`, при flatten —
  закрытие позиций reduce_only-ордерами с аудитом (`KILL_SWITCH`). Код на включение не нужен.
- Защитные выходы SL/TP (`check_protective_exits`) срабатывают всегда, в том числе при
  `TRIPPED` и `KILLED`.
- `Reconciler`: сверяет активные ордера и позиции БД с площадкой. Ордер, которого площадка не
  знает → REJECTED. Расхождение позиций > `qty_step` → `RECONCILE_MISMATCH` и срабатывание CB.
- `risk/post_trade.py`: `RiskMetrics`, чистая `evaluate_triggers`, `RiskMetricsBuilder` (SQLite),
  `PostTradeMonitor` (после fill: снимок → метрики → CB).
- `ResilientAdapter` (ARCH §4.2): повторы для чтения и идемпотентных операций; `place_order` —
  одна попытка с таймаутом. Хук `on_error` считает ошибки площадки для CB (`RejectedError` не
  считается).
- `ModeStore` — минимальная запись `mode_state` + `MODE_CHANGED`: только понижение и PAPER после
  reset. Повышение с кодами (`ModeController`) — 0.7.0.
- `TradingPipeline` и `build_paper_stack` — сборка PAPER: восстановление PaperAdapter из SQLite
  после рестарта, снимок портфеля перед каждым решением (R10).
- CLI: `augur kill-switch [--flatten/--no-flatten]`, `augur cb status`, `augur cb reset --confirm`,
  `augur orders list`.
- Миграция `0005_execution.sql` (`expand`): `fills` (DDL ARCH §3.2).
- Тесты: state machine, `client_order_id`, издержки, `ResilientAdapter`, позиции и снимки,
  восстановление после сбоев сети, частичные исполнения, CB / kill switch с рестартом, reset по
  коду, Reconciler, CLI, e2e PAPER.

### Изменено

- Модель издержек вынесена в `augur.backtest.costs.CostModel` и теперь общая для `Backtester` и
  `PaperAdapter` (ARCH §4.6, §6.3). Результаты бэктеста не изменились.
- `PaperAdapter`: исполняет по bid/ask с проскальзыванием и списывает комиссию; отдаёт `fills`,
  `fee`, `fee_ccy`; отклоняет `reduce_only`, если ордер увеличил бы позицию; `restore()` и
  `set_clock()`; у синтетического tick есть «24h-объём» (без него R9 в PAPER всегда отклонял
  сигнал). Издержки по умолчанию нулевые, PAPER-стек берёт их из конфига.
- `OrderAck`: новые поля `fee`, `fee_ccy`, `fills: list[VenueFill]`, `reject_reason` (все с
  дефолтами). Новый DTO `VenueFill`.
- `call_with_retry`: необязательный параметр `on_error`.
- Конфиг (config_version 3): `ExecutionSettings` (`EXECUTION__*`: комиссия, проскальзывание,
  `partial_timeout_s`, `reconcile_interval_s`, `paper_start_cash`), `CircuitBreakerSettings`
  (`CB__*`: `flatten_on_trip`, окно и порог ошибок адаптера, TTL кода сброса),
  `FusionSettings.invalid_ratio` / `invalid_window`. У всех полей есть дефолты, `.env` менять не
  нужно.
- `DB_SCHEMA_VERSION`: 4 → 5; `CONFIG_VERSION`: 2 → 3.
- VERSIONING.md §4: объём 0.6.0 / 0.7.0 приведён к ARCH §14 (решение пользователя); §2.2 —
  константы.
- Skill `auto-trade-risk-gate`: freeze, kill switch и сброс приведены к ARCH §7.4 (`cb reset
  --confirm` → PAPER, автосброса нет); `client_order_id` из `(signal_id, leg, attempt)`;
  порядок записи `decision_audit` — по VERSIONING §6.2.
- CLAUDE.md: команды `kill-switch`, `cb status|reset`.

### Безопасность

- Kill switch и CB персистентны и переживают рестарт; снять их можно только одноразовым кодом
  из CLI.
- Ордер без закоммиченной полной записи `decision_audit` не создаётся и не отправляется
  (`UnauditableOrderError` / `ExecutionRefusedError`).
- В 0.6.0 `OrderManager` отказывается работать с адаптером, отличным от `paper`, и в режимах
  MICRO_LIVE / FULL_AUTO.

### Примечания

- Системные выходы (SL/TP, flatten) снижают риск и идут мимо сигнального контура и Risk Gate —
  так требует ARCH §7.4. Для них пишутся собственные `signals(kind='final', action='CLOSE')` и
  `decision_audit` с полным набором версий; ордер только `reduce_only`.
- `[ДОПУЩЕНИЕ]` Счётчики серий (убытки, невалидные fused_signal) и пик для просадки считаются с
  последнего `cb reset`. PnL сделки для серии убытков = realized PnL закрывающего fill. День для
  `day_pnl` — UTC.
- `[ДОПУЩЕНИЕ]` Расхождение позиций в PAPER тоже вызывает CB (ARCH §7.4 называет только live):
  БД и PaperAdapter обязаны совпадать точно.
- `DbWriter` (single writer, ARCH §3.1) отложен до оркестратора (решение пользователя); пока
  используются синхронные транзакции `sqlite3`.
- Notify ещё не реализован: CRITICAL-события CB и kill switch пишутся в `audit_log` и
  `logging.critical`.
- Пример short-circuit в skill `auto-trade-risk-gate` (раздел Examples) всё ещё расходится с
  ARCH §7.2 (код следует ARCH) — не исправлялся.

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