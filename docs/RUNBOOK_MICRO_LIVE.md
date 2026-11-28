# Runbook: MICRO_LIVE (0.7.0)

Прогон на реальном счёте с минимальным депозитом (ARCHITECTURE.md §14, этап 7). Выполняет человек.
Агент не вводит ключи и не отправляет реальные ордера.

## 0. Предусловия

- Выделенный **субсчёт** биржи. На входе в режим в нём нет базовых активов торгуемых символов
  (`settings.symbols`), только валюта котировки (USDT) в размере минимального депозита.
- API-ключ этого субсчёта: только торговля spot, **без права вывода**, с привязкой к IP.
- Ключи — только в `.env` (`CCXT_API_KEY`, `CCXT_API_SECRET`). В коде, логах и коммитах их нет.
- `pip install -e ".[dev,ccxt]"`, `augur db migrate`, затем `pytest` и `ruff check .` проходят.
- Бэкап БД: `augur db backup`.

## 1. Testnet

```env
TRADING_MODE=MICRO_LIVE
MAX_ALLOWED_MODE=MICRO_LIVE
CCXT_EXCHANGE=binance
CCXT_SANDBOX=true
CCXT_API_KEY=...   # ключ testnet
CCXT_API_SECRET=...
RISK__MICRO_LIVE_MAX_NOTIONAL=20
```

1. `augur mode show`: режим PAPER, CB ARMED.
2. `augur mode set MICRO_LIVE --confirm`. CLI сверит позиции с площадкой, напечатает `TESTNET`,
   выдаст код. Введите код.
3. Дождитесь первого одобренного сигнала: ордер в `PENDING`, в `augur mode show` он в строке
   «awaiting orders».
4. `augur orders approve <id> --confirm`: проверьте сторону, объём и символ, введите код.
   Ожидаемо `FILLED`, notional ≤ `micro_live_max_notional`.
5. `augur reconcile`: без `MISMATCH`.
6. `augur kill-switch --flatten`: позиция закрыта, режим ANALYSIS_ONLY.
   `augur cb reset --confirm` → PAPER.

Если биржа не принимает `clientOrderId` или не находит ордер по нему (`get_order` → не найден
у исполненного ордера), остановитесь. Нужен маппинг `ccxt_venue_quirks.py` (ARCH §4.4).

## 2. Реальный счёт

То же, но `CCXT_SANDBOX=false` и ключ субсчёта. CLI должен печатать `REAL MONEY`.

- Первые сутки после каждого ордера запускайте `augur reconcile`. С 0.11.0 `augur run` сверяет
  сам каждые `EXECUTION__RECONCILE_INTERVAL_S`; инциденты и переход в FULL_AUTO —
  `docs/RUNBOOK_INCIDENTS.md`.
- SL/TP на spot биржа не держит: защитные выходы срабатывают, только пока работает процесс
  (`check_protective_exits`).
- Любое расхождение → CB `reconcile_mismatch`. Разберитесь вручную, затем `augur cb reset --confirm`
  и снова `mode set MICRO_LIVE`.

## 3. Откат (VERSIONING.md §8)

Перед релизом 0.7.0 отрепетируйте откат на копии боевой БД (VERSIONING.md §1.3, п. 5).

Аварийно: `augur kill-switch --flatten`. Если площадка недоступна, CLI всё равно выставит
KILLED и предупредит, что ордера и позиции на площадке нужно закрыть вручную в интерфейсе биржи.
