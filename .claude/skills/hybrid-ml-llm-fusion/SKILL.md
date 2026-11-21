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
3. Проверь, что `FusedSignal` — Pydantic-модель с полями из §4: `action`, `strength`, `source_breakdown`, `explanation_ref`, `risk_flags`, `ttl_seconds`, а также `created_at`, без которого TTL нельзя проверить. `model_config = ConfigDict(frozen=True)`.
4. Веса и пороги бери только из `FusionConfig`. Конфиг валидируется при старте: сумма весов = 1, все пороги заданы. В `fuse()` нет числовых литералов, кроме 0 и 1.
5. Реализуй `fuse()` как чистую детерминированную функцию `(ml_pred, llm_ctx, tech_signal, cfg) -> FusedSignal | None`. Никакого I/O и вызовов LLM внутри.
6. Применяй veto в фиксированном порядке:
   1. `ml_pred.confidence < cfg.ml_min_confidence` → вернуть `None` (сигнал отброшен), причину записать в audit.
   2. `"high" in llm_ctx.risk_flags` → `action=HOLD`, вход заблокирован, флаг перенести в `risk_flags`.
   3. Направления ML и LLM противоречат друг другу → приоритет у risk-контура: `action=HOLD`, добавить `risk_flags += ["ml_llm_conflict"]`.
7. Проведи constraint-based verification (§5) после `fuse()`:
   - `0 <= strength <= 1`, `ttl_seconds > 0`;
   - `action` согласован с `source_breakdown`;
   - ML-компонента в `source_breakdown` равна исходному значению из `ml_predictions`;
   - `explanation_ref` указывает на существующую строку `llm_calls`.
   Нарушение любого инварианта делает сигнал невалидным (см. шаг 10).
8. Anti-sycophancy:
   - LLM вызывается до слияния и независимо: в промпте нет ML-решения, итогового `action`, открытых позиций;
   - Fusion не подстраивает ML-сигнал под вывод LLM;
   - LLM не влияет на размер позиции и на Risk Gate.
9. TTL: потребитель отбрасывает сигнал, если `now - created_at > ttl_seconds`, и пишет причину в audit.
10. Fallback: если Fusion недоступен, бросил исключение или вернул невалидный `FusedSignal`, Signal Engine переходит в режим ML-disabled (только классические стратегии) и пишет WARNING в `audit_log` с причиной. Не падай и не повторяй вызов в цикле. Серию невалидных сигналов учитывает circuit breaker (`auto-trade-risk-gate`).
11. Сохраняй связи: каждый `fused_signal` ссылается на `ml_predictions.id` и `llm_calls.id`.
12. Тесты: каждое veto, конфликт, TTL, fallback, property-test инвариантов шага 7. Запусти `pytest tests/fusion`.

## Examples
```python
# src/augur/fusion/models.py
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class FusedSignal(BaseModel):
    model_config = ConfigDict(frozen=True)
    action: Literal["BUY", "SELL", "HOLD"]
    strength: float = Field(ge=0, le=1)
    source_breakdown: dict[str, float]      # {"ml": .., "llm": .., "tech": ..}
    explanation_ref: str | None             # llm_calls.id
    risk_flags: list[str] = []
    ttl_seconds: int = Field(gt=0)
    created_at: datetime
```

```python
# src/augur/fusion/engine.py
def fuse(ml, llm, tech, cfg: FusionConfig, now: datetime) -> FusedSignal | None:
    if ml.confidence < cfg.ml_min_confidence:
        return None                                   # veto: weak ML
    flags = list(llm.risk_flags)
    score = cfg.w_ml * ml.score + cfg.w_llm * llm.score + cfg.w_tech * tech.score
    action = cfg.to_action(score)
    if "high" in flags or (ml.direction != llm.direction != "NEUTRAL"):
        flags.append("ml_llm_conflict" if "high" not in flags else "llm_high_risk")
        action = "HOLD"                               # risk-контур важнее
    return FusedSignal(action=action, strength=abs(score),
                       source_breakdown={"ml": ml.score, "llm": llm.score, "tech": tech.score},
                       explanation_ref=llm.call_id, risk_flags=flags,
                       ttl_seconds=cfg.ttl_seconds, created_at=now)
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
- **`ttl_seconds` игнорируется.** Устаревший сигнал исполняется по рынку, которого уже нет.
- **LLM видит итоговое решение или позицию.** Модель начинает рационализировать решение вместо независимой оценки.
- **Исключение в Fusion роняет Signal Engine.** Должен включаться режим ML-disabled с WARNING, а не простой торговли.