# Runbook: FULL_AUTO и инциденты (0.11.0)

Переход MICRO_LIVE → FULL_AUTO и действия при инцидентах (ARCHITECTURE.md §1.4, §7.4, §14 этап 11).
Выполняет человек. Агент не вводит ключи, не вводит коды подтверждения и не включает реальные деньги.

Общие правила:

- Первое действие при любом непонятном поведении на реальных деньгах — `augur kill-switch --flatten`.
  Kill switch не требует кода. Разбор — потом.
- Автосброса нет: CB и kill switch снимаются только `augur cb reset --confirm` с кодом, после чего
  режим = PAPER. Эскалация снова идёт по шагам через `augur mode set`.
- Каждое действие пишется в `audit_log`. Проверка целостности — `augur audit verify-chain`.

## 0. Алерты (обязательны для FULL_AUTO)

```env
TELEGRAM_BOT_TOKEN=...   # только в .env
TELEGRAM_CHAT_ID=...
NOTIFY__MIN_LEVEL=WARN   # INFO | WARN | CRITICAL
```

1. `augur alerts test` — сообщение пришло в чат.
2. Источник алертов — `audit_log`. Алертами становятся события `CB_TRIPPED`, `KILL_SWITCH`,
   `RECONCILE_MISMATCH` (CRITICAL), `MODE_CHANGED` (переход в FULL_AUTO — CRITICAL), `CB_RESET`,
   `ORDER_APPROVAL_REQUIRED`, `LLM_BREAKER_OPENED`, `ML_MODEL_UNAVAILABLE`, `NEWS_REACTION`,
   `RUN_STOPPED` (WARN), `RUN_ERROR` (CRITICAL), а также любое событие с severity ERROR/CRITICAL.
3. Отправляет `augur run` (каждые `RUN__ALERTS_S` секунд) или вручную `augur alerts dispatch`.
   Журнал — `augur alerts list [--status failed]`. Сбой Telegram торговлю не останавливает:
   алерт повторяется с backoff, после `NOTIFY__MAX_ATTEMPTS` попыток получает статус `failed`.
4. История до миграции 0008 не рассылается.

## 1. Переход MICRO_LIVE → FULL_AUTO

Предусловия:

- MICRO_LIVE прошёл по `docs/RUNBOOK_MICRO_LIVE.md` на реальном счёте.
- В `.env`: `TRADING_MODE=FULL_AUTO`, `MAX_ALLOWED_MODE=FULL_AUTO`. Конфиг только разрешает режим,
  сам он не включается.
- Бэкап: `augur db backup`.

Шаги:

1. `augur mode promotion` — все пункты выполнены (`READY`):
   - исполнено не менее `MODE__MIN_MICRO_TRADES` (20) ордеров MICRO_LIVE с момента входа в режим;
   - за это время не было `CB_TRIPPED` / `KILL_SWITCH`;
   - сверка с моделью издержек бэктеста: не менее `MODE__MIN_COMPARED_ORDERS` (10) сигнальных
     ордеров сравнены с `CostModel` по close бара сигнала, средний перекос (проскальзывание +
     комиссия) не больше `MODE__MAX_COST_EXCESS_BPS` (15 б.п.). Ордер без бара в `ohlcv`
     не засчитывается;
   - нет активных ордеров MICRO_LIVE (книга переносится в FULL_AUTO одной транзакцией);
   - алерты настроены.
2. `augur mode set FULL_AUTO --approved-by <имя> --confirm`. CLI печатает отчёт и код; введите код.
   В `MODE_CHANGED` пишутся `approved_by`, сводка promotion и перенос позиций (`book_transfer`).
   Переход утверждает один человек (решение 0.11.0).
3. `augur run` — ордера уходят автоматически в пределах Risk Gate, без ручного подтверждения.
   R15 в FULL_AUTO блокирует и `fusion_contrarian` (как в MICRO_LIVE).
4. Первые сутки: `augur mode show`, `augur alerts list`, `augur reconcile` вручную раз в несколько
   часов (оркестратор тоже сверяет каждые `EXECUTION__RECONCILE_INTERVAL_S`).

Понижение: `augur mode set MICRO_LIVE --confirm` (книга вернётся в MICRO_LIVE; при активных
ордерах переход запрещён — ждите их завершения или понижайте до PAPER) или
`augur mode set PAPER --confirm` (сразу, без кода).

## 2. Инциденты

### 2.1. CB_TRIPPED

Алерт CRITICAL. Новые входы заблокированы, ордера на вход отменены, режим → PAPER (PAPER-hold).
Позиции не закрыты (если не включено `CB__FLATTEN_ON_TRIP`), SL/TP продолжают работать, пока
запущен `augur run`.

1. `augur cb status` — триггер и деталь.
2. По триггеру:
   - `drawdown` / `daily_loss` / `consecutive_losses` — разберите сделки (`augur orders list`,
     `augur signals explain <id>`). Решите, закрывать ли позиции: `augur kill-switch --flatten`.
   - `adapter_errors` — проверьте связь с биржей и статус API площадки.
   - `fusion_invalid_streak` — `augur llm usage`, `augur fusion replay <id>`; при сомнениях
     `FUSION__ENABLED=false` (классика) до разбора.
   - `reconcile_mismatch` — см. 2.3.
3. Причина устранена → `augur cb reset --confirm` → PAPER → эскалация заново по шагам. Возврат в
   MICRO_LIVE снова требует субсчёта без базовых активов (сверка при `mode set MICRO_LIVE`).

### 2.2. KILL_SWITCH

`augur kill-switch --flatten` (или кнопка в GUI): KILLED, ANALYSIS_ONLY, отмена ордеров, закрытие
позиций reduce_only-ордерами. Если площадка недоступна, CLI всё равно выставит KILLED и предупредит:
закройте ордера и позиции вручную в интерфейсе биржи. Снятие — `augur cb reset --confirm`.

### 2.3. RECONCILE_MISMATCH

Позиции в БД и на площадке разошлись → CB. Не сбрасывайте CB, пока не поймёте причину.

1. `augur reconcile` — какие символы и объёмы.
2. Сравните с историей ордеров в интерфейсе биржи. Частые причины: ручная сделка на субсчёте,
   ордер в `UNKNOWN` после обрыва связи, комиссия списана базовым активом.
3. Приведите субсчёт в согласованное состояние (закройте лишнее вручную), затем
   `augur reconcile` без `MISMATCH` → `augur cb reset --confirm`.

### 2.4. LLM_BREAKER_OPENED

Доля отклонённых LLM-ответов за час выше порога: Fusion работает в ML-only (меньший размер).
Торговля продолжается. `augur llm usage` — статусы вызовов; проверьте провайдера, бюджет
(`FUSION__LLM__DAILY_USD`) и ключ. Breaker закрывается сам, когда отклонённые вызовы выходят из
часового окна (`LLM_BREAKER_CLOSED`). Состояния HALF_OPEN с пробными вызовами (FUSION SPEC §5.4)
пока нет.

### 2.5. RUN_ERROR / RUN_STOPPED

Задача оркестратора упала (не чаще одного алерта в 15 минут на задачу) или процесс остановился.
Посмотрите лог процесса, `augur alerts list`. Пока `augur run` остановлен, SL/TP на spot не
срабатывают: биржа их не держит. Если процесс не поднять быстро, закройте позиции:
`augur kill-switch --flatten`.

### 2.6. Площадка недоступна

`ResilientAdapter` повторяет вызовы, серия ошибок → CB `adapter_errors`. Ордер с неизвестным
статусом — `UNKNOWN`, R5 блокирует символ до сверки. После восстановления: `augur reconcile`.

## 3. Откат версии

VERSIONING.md §8: бэкап до миграции 0008 → откат кода → восстановление бэкапа → запуск в PAPER.
Миграция 0008 — `expand` (новые таблицы `notifications`, `notify_cursor`), код 0.10.0 её терпит.
