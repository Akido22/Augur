---
name: auto-trade-risk-gate
description: Pre-trade checks, circuit breaker with position freeze, manual kill switch, PAPER / MICRO_LIVE / FULL_AUTO trading modes, order idempotency via client_order_id and versioned decision_audit for every order attempt in Augur. Use when creating or modifying src/augur/risk/*, src/augur/execution/*, exchange/broker adapters, or any code that sends, cancels or sizes orders or switches trading modes. Do NOT use for producing signals (use hybrid-ml-llm-fusion) or for validating LLM output (use llm-output-validation).
---

# auto-trade-risk-gate

Последний детерминированный барьер перед биржей. Каждый ордер проходит pre-trade checks и пишется в `decision_audit`. Защитные выходы (SL/TP) не блокируются никогда.

## When to use
- Создаёшь или меняешь `src/augur/risk/*`, `src/augur/execution/*`, адаптеры бирж и брокеров.
- Пишешь код, который отправляет, отменяет или модифицирует ордера или считает размер позиции.
- Меняешь режимы PAPER / MICRO_LIVE / FULL_AUTO, circuit breaker, kill switch.

When NOT to use:
- Генерация или слияние сигналов → `hybrid-ml-llm-fusion`.
- Парсинг выхода LLM → `llm-output-validation`.
- Версии, теги, миграции → `release-management`.

## Procedure
1. Прочитай `ARCHITECTURE.md`, разделы Risk Gate, Execution и режимы. Поля `decision_audit` возьми из `VERSIONING.md`.
2. Pre-trade checks выполняй в одной функции. Любой провал означает отказ с причиной. Лимиты берутся из конфига, не из кода:
   - размер позиции ≤ `max_position_pct` equity;
   - дневной убыток < `daily_loss_limit_pct` (типично 4–5% equity);
   - концентрация по инструменту и классу активов;
   - ликвидность (объём, глубина стакана) и спред ≤ `max_spread_bps`;
   - TTL `fused_signal` не истёк;
   - режим разрешает вход, kill switch выключен, CB не сработал.
3. Circuit breaker срабатывает, если:
   - N последовательных убытков;
   - просадка выше лимита;
   - серия ошибок адаптера;
   - серия невалидных `fused_signal`.
   CB останавливает только новые входы. SL/TP и закрытие существующих позиций продолжают работать.
4. Position freeze (ARCH §7.4):
   - при срабатывании CB `cb_state = TRIPPED` (персистентно): Risk Gate R2 отклоняет новые входы, открытые ордера на вход отменяются, режим → PAPER-hold;
   - CLOSE/REDUCE по сигналам и защитные SL/TP продолжают работать (`OrderManager.check_protective_exits`);
   - позиции не закрываются автоматически, если не включено `cb.flatten_on_trip`.
   Автоматического сброса нет: только `augur cb reset --confirm` с одноразовым кодом → режим PAPER, запись `CB_RESET` в `audit_log`.
5. Kill switch: `augur kill-switch [--flatten/--no-flatten]` в CLI и кнопка в GUI, без кода подтверждения на включение. `cb_state = KILLED` (персистентно, переживает рестарт), режим → ANALYSIS_ONLY, `cancel_all`, при flatten — закрытие позиций reduce_only-ордерами с аудитом. Снятие — тем же `augur cb reset --confirm` с кодом → PAPER; повторная эскалация — через `augur mode set`. Автоматического снятия нет.
6. Идемпотентность:
   - `client_order_id` генерируется детерминированно из `(signal_id, leg, attempt)` (ARCH §7.3);
   - перед отправкой проверь `orders` на дубликат;
   - при сетевой ошибке сначала запроси статус по `client_order_id`, только потом повторяй отправку.
7. `decision_audit` пишется на каждую попытку, включая отклонённые. Обязательные поля:
   - `app_version`, `strategy_version`, `ml_model_version`, `llm_prompt_version`;
   - `risk_verdict`, `risk_reason`, `mode`; `client_order_id` — в `execution_result` (VERSIONING §6.1).
   Нет записи в audit — нет ордера: `decision_audit` коммитится до отправки (VERSIONING §6.2); PENDING-ордер и `decision_audit.order_id` — одной транзакцией, затем отправка.
8. Режимы:
   - PAPER по умолчанию;
   - MICRO_LIVE: минимальный размер и ручное подтверждение первого ордера;
   - FULL_AUTO: только после успешного MICRO_LIVE (критерии в конфиге).
   Каждая смена режима пишется в `audit_log` с автором и причиной.
9. Тесты: каждый check, CB при открытой позиции (SL/TP срабатывает), повторная отправка (одна сделка), kill switch после рестарта, запрет FULL_AUTO без MICRO_LIVE.

## Examples
```python
def pre_trade_check(sig: FusedSignal, ctx: RiskContext, cfg: RiskConfig) -> RiskVerdict:
    checks = [
        (ctx.kill_switch_on, "kill_switch"),
        (ctx.daily_loss_limit_reached, "circuit_breaker"),
        (sig.is_expired(ctx.now), "signal_ttl_expired"),
        (ctx.position_pct_after(sig) > cfg.max_position_pct, "position_size"),
        (ctx.daily_loss_pct >= cfg.daily_loss_limit_pct, "daily_loss"),
        (ctx.concentration_after(sig) > cfg.max_concentration, "concentration"),
        (ctx.spread_bps > cfg.max_spread_bps, "spread"),
        (ctx.liquidity < cfg.min_liquidity, "liquidity"),
    ]
    for failed, reason in checks:
        if failed:
            return RiskVerdict(allowed=False, reason=reason)
    return RiskVerdict(allowed=True, reason="ok")
```

```python
async def on_tick(self, prices):
    await orders.check_protective_exits(prices)   # всегда, даже при TRIPPED / KILLED
    for sig in ready_signals:
        await pipeline.process(sig)                # RiskGate R2 сам отклонит вход при CB
```

```bash
augur kill-switch --flatten       # KILLED, ANALYSIS_ONLY, отмена ордеров, закрытие позиций
augur cb status                   # состояние CB и режим
augur cb reset --confirm          # одноразовый код → ARMED, режим PAPER
```

## Anti-patterns
- **Блокировать stop-loss при срабатывании CB.** Защита превращается в риск: открытая позиция остаётся без выхода как раз в плохой день.
- **Сбрасывать CB автоматически без подтверждения.** Причина срабатывания (сбой адаптера, деградация модели) не устранена, и серия убытков продолжается.
- **Ордер без хотя бы одного поля версии в `decision_audit`.** Решение невозможно воспроизвести, и при разборе инцидента непонятно, какой код и какая модель торговали.
- **Переключение режима без записи в `audit_log`.** Нельзя доказать, кто и когда включил реальные деньги.
- **Повтор отправки без проверки статуса по `client_order_id`.** Сетевой таймаут превращается в двойную позицию.
- **Лимиты риска в коде вместо конфига.** Их нельзя проверить, версионировать и изменить без релиза.