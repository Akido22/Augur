---
name: hybrid-ml-llm-fusion
description: Rules for fusing an ML forecast, validated LLM context and a technical signal into a Pydantic fused_signal in Augur - config-driven weights, veto rules (LLM risk_flag=high, low ML confidence), constraint-based verification, anti-sycophancy, TTL enforcement and ML-disabled fallback. Use when creating or modifying src/augur/fusion/*, the Signal Engine, fusion weights/thresholds, the FusedSignal model, or any code that combines ML and LLM signals. Do NOT use for parsing raw LLM output (use llm-output-validation) or for position sizing, order sending and trading modes (use auto-trade-risk-gate).
---

# hybrid-ml-llm-fusion

Слияние ML-прогноза, LLM-контекста и технического сигнала в `fused_signal`. Fusion только предлагает сигнал. Решение об исполнении принимает `auto-trade-risk-gate`.

## When to use
- Создаёшь или меняешь `src/augur/fusion/*`.
- Меняешь Signal Engine, веса слияния, пороги confidence или veto-правила.
- Меняешь модель `FusedSignal` или её потребителей.

When NOT to use:
- Парсинг и валидация сырого ответа LLM → `llm-output-validation`.
- Размер позиции, лимиты, ордера, режимы → `auto-trade-risk-gate`.
- Повышение версии контракта `fused_signal`, CHANGELOG → `release-management`.

## Procedure
1. Прочитай `ML_LLM_FUSION_SPEC.md`: §3 (LLM-слой), §4 (Signal Fusion), §5 (верификация). Не копируй спеку в код. В докстрингах ссылайся на раздел.
2. Убедись, что LLM-вход прошёл `llm-output-validation` (schema + variance filter). Если нет, сначала примени его.
3. Используй `augur.fusion.contract.FusedSignal` — не переопределяй модель. Схема — `ML_LLM_FUSION_SPEC.md` §4.6 (источник истины): `api_version`, `signal_id`, `idempotency_key`, `asset`, `timestamp`, `expires_at`, `action` (6 значений `SignalAction`), `strength`, `reason`, `explanation_ref`, `risk_flags` (`{code, severity}`), `source_breakdown` (`float | list[str] | dict`), `fusion_config_version`. `model_config = ConfigDict(frozen=True, extra="forbid")`.
4. Веса и пороги бери только из `FusionConfig`. Конфиг валидируется при старте: сумма весов = 1, все пороги заданы. В `fuse()` нет числовых литералов, кроме 0 и 1.
5. Реализуй `fuse()` как чистую детерминированную функцию `(ml_pred, llm_ctx, tech_signal, cfg) -> FusedSignal | None`. Никакого I/O и вызовов LLM внутри.
6. Применяй veto в фиксированном порядке:
   1. `ml_pred.confidence < cfg.ml_min_confidence` → вернуть `None` (сигнал отброшен), причину записать в audit.
   2. В `llm_ctx.risk_flags` есть флаг с `severity="high"` → `action=HOLD`, вход заблокирован, флаг перенести в `risk_flags` как `{code, severity}`.
   3. Направления ML и LLM противоречат друг другу → приоритет у risk-контура: `action=HOLD`, добавить `"ml_llm_conflict"` в `source_breakdown["rules_fired"]` (системные метки — не `risk_flags`, FUSION SPEC §4.5).
7. Проведи constraint-based verification (§5) после `fuse()`:
   - `0 <= strength <= 1`, `expires_at > timestamp`, `api_version == versions.API_VERSION`;
   - `action` согласован с `source_breakdown`;
   - ML-компонента в `source_breakdown` равна исходному значению из `ml_predictions`;
   - `explanation_ref` — непустая строка-ссылка по §4.6 (`"fusion_stub"` / `"decision_audit:<ulid>"`), не FK; ссылки на `llm_calls` — в `source_breakdown["llm"]`.
   Нарушение любого инварианта делает сигнал невалидным (см. шаг 10).
8. Anti-sycophancy:
   - LLM вызывается до слияния и независимо: в промпте нет ML-решения, итогового `action`, открытых позиций;
   - Fusion не подстраивает ML-сигнал под вывод LLM;
   - LLM не влияет на размер позиции и на Risk Gate.
9. TTL: потребитель отбрасывает сигнал, если `now >= expires_at`, и пишет причину в audit (`FUSION_INVALID`, ARCHITECTURE.md §5.5 V5).
10. Fallback: если Fusion недоступен, бросил исключение или вернул невалидный `FusedSignal`, Signal Engine переходит в режим ML-disabled (только классические стратегии) и пишет WARNING в `audit_log` с причиной. Не падай и не повторяй вызов в цикле. Серию невалидных сигналов учитывает circuit breaker (`auto-trade-risk-gate`).
11. Сохраняй связи: каждый `fused_signal` ссылается на `ml_predictions.id` и `llm_calls.id`.
12. Тесты: каждое veto, конфликт, TTL, fallback, property-test инвариантов шага 7. Запусти `pytest tests/fusion`.

## Examples
```python
# src/augur/fusion/contract.py — модель уже есть, не дублировать (FUSION SPEC §4.6)
from augur.fusion.contract import FusedSignal, RiskFlag

RiskFlag(code="exchange_incident", severity="medium")   # risk_flags — объекты, не строки
```

```python
# src/augur/fusion/engine.py — эскиз; полные правила и порядок veto — ML_LLM_FUSION_SPEC.md §4.3–4.5
def fuse(ml, llm, tech, cfg: FusionConfig, *, req: FusionRequest, now_ms: int, bar_ms: int) -> FusedSignal:
    rules: list[str] = []                                   # -> source_breakdown["rules_fired"]
    risk_flags = [RiskFlag(code=f.code.value, severity=f.severity.value) for f in llm.risk_flags]
    score = cfg.weights.ml * ml.score + cfg.weights.llm * llm.score + cfg.weights.tech * tech.score
    action = cfg.to_action(score)                           # -> SignalAction
    if any(f.severity == "high" for f in risk_flags):
        rules.append("llm_high_risk_flag")
        action = SignalAction.HOLD                          # risk-контур важнее
    return FusedSignal(
        api_version=versions.API_VERSION, signal_id=..., idempotency_key=req.idempotency_key,
        asset=req.asset, timestamp=req.timestamp,
        expires_at=now_ms + int(bar_ms * cfg.signal_ttl_fraction_of_bar),
        action=action, strength=min(abs(score), 1.0), reason=rules[-1] if rules else "ok",
        explanation_ref="decision_audit:<ulid>",            # ULID заранее выдаёт Signal Engine (0.5.0+)
        risk_flags=risk_flags,
        source_breakdown={"ml": ml.score, "tech": tech.score, "llm": llm.score, "rules_fired": rules},
        fusion_config_version=cfg.config_version,
    )
```

```python
# Signal Engine: fallback
try:
    sig = verify(fuse(ml, llm, tech, cfg, now))
except (FusionError, ValidationError) as e:
    audit.warning("fusion_fallback", reason=str(e))
    sig = classic_strategies.signal(market)          # ML-disabled
```

## Anti-patterns
- **LLM напрямую влияет на размер позиции или Risk Gate.** Недетерминированный и неаудируемый компонент начинает управлять риском, а галлюцинация превращается в объём сделки.
- **Веса захардкожены в `fuse()`.** Их нельзя версионировать, нельзя воспроизвести решение по `decision_audit`, а любая подстройка требует релиза кода.
- **Fusion подстраивает ML-сигнал под вывод LLM (sycophancy).** Два источника сливаются в один, и гибрид теряет смысл: ошибка LLM подтверждается, а не гасится.
- **`expires_at` игнорируется.** Устаревший сигнал исполняется по рынку, которого уже нет.
- **LLM видит итоговое решение или позицию.** Модель начинает рационализировать решение вместо независимой оценки.
- **Исключение в Fusion роняет Signal Engine.** Должен включаться режим ML-disabled с WARNING, а не простой торговли.