---
name: llm-output-validation
description: Hallucination defense for every LLM output in Augur - strict Pydantic schema validation with reject-not-repair, multi-sample variance filter, optional adversarial critic, mechanical separation (LLM only proposes TradeProposal, deterministic code decides), budget/latency fallback to ML-only. Use when creating or modifying src/augur/llm/validators.py, src/augur/llm/client.py, prompts, or any code that parses, stores or consumes LLM output. Do NOT use for combining validated LLM context with ML signals (use hybrid-ml-llm-fusion) or for order execution (use auto-trade-risk-gate).
---

# llm-output-validation

Защита от галлюцинаций LLM. Невалидный выход отбрасывается и никогда не «чинится». LLM только предлагает, решает детерминированный код.

## When to use
- Создаёшь или меняешь `src/augur/llm/validators.py`, `src/augur/llm/client.py`, промпты.
- Пишешь код, который парсит, сохраняет в `llm_calls` или передаёт дальше выход LLM.
- Меняешь бюджеты, таймауты или число сэмплов LLM.

When NOT to use:
- Слияние уже валидного LLM-контекста с ML → `hybrid-ml-llm-fusion`.
- Ордера, лимиты, режимы → `auto-trade-risk-gate`.

## Procedure
1. Прочитай `ML_LLM_FUSION_SPEC.md` §3 (LLM-слой) и §5 (верификация, variance filter, anti-sycophancy).
2. Опиши выход LLM Pydantic-моделью:
   - `action: Literal["BUY", "SELL", "HOLD"]`;
   - `amount: float = Field(ge=0)`;
   - `reasoning: str = Field(min_length=1)`;
   - `model_config = ConfigDict(extra="ignore", frozen=True)`.
3. Парсинг: `TradeProposal.model_validate_json(raw)`. При `ValidationError` ответ отбрасывается и записывается в `llm_calls` со статусом `rejected` и причиной. Никаких регулярок, `json_repair`, синонимов.
4. Variance filter: N сэмплов (N из конфига) одного промпта. Если action не совпадает по большинству или дисперсия числовых полей выше `cfg.max_variance`, сигнал отбрасывается.
5. Adversarial critic (опционально, флаг в конфиге): второй вызов в роли критика получает только proposal и входные данные и ищет противоречия в `reasoning`. Вердикт `fail` означает отбраковку. Критик не правит proposal.
6. Механическое разделение: на выходе слоя только `TradeProposal` или `None`. Решение об исполнении принимает детерминированный код (`hybrid-ml-llm-fusion` → `auto-trade-risk-gate`).
7. После валидации объект неизменяем (`frozen=True`). Числовые поля (`amount`, `confidence`) не перезаписываются ни LLM, ни повторным вызовом.
8. Fallback: при превышении бюджета токенов или стоимости, таймауте, ошибке провайдера или отбраковке всех сэмплов вернуть `None`. Вызывающий код работает в режиме ML-only. Записать WARNING в `audit_log`.
9. Логирование в `llm_calls`: `llm_prompt_version`, модель, latency, стоимость, статус (`ok` / `rejected` / `timeout` / `over_budget`), ссылка на сырой ответ.
10. Тесты: невалидный JSON, неизвестный action (`"ACQUIMULATE"`), отрицательный amount, пустой reasoning, высокая дисперсия, таймаут. Каждый случай должен давать `None`, а не исправленное значение.

## Examples
```python
# src/augur/llm/validators.py
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError

class TradeProposal(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    action: Literal["BUY", "SELL", "HOLD"]
    amount: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    reasoning: str = Field(min_length=1)
    risk_flags: list[Literal["low", "medium", "high"]] = []

def parse(raw: str) -> TradeProposal | None:
    try:
        return TradeProposal.model_validate_json(raw)
    except ValidationError as e:
        record_rejection(raw, reason=str(e))
        return None                      # reject, never repair
```

```python
def variance_filter(samples: list[TradeProposal], cfg) -> TradeProposal | None:
    if len(samples) < cfg.min_valid_samples:
        return None
    actions = Counter(s.action for s in samples)
    top, count = actions.most_common(1)[0]
    if count / len(samples) < cfg.min_agreement:
        return None
    if pstdev([s.confidence for s in samples]) > cfg.max_variance:
        return None
    return next(s for s in samples if s.action == top)
```

## Anti-patterns
- **Чинить невалидный JSON регуляркой вместо отбраковки.** Система выдумывает ответ, которого модель не давала, а аудит записывает его как решение LLM.
- **Маппинг `"ACQUIMULATE"` → `BUY`.** Это решение системы, а не модели: неизвестное значение говорит о сбое, и интерпретировать его как торговый сигнал опасно.
- **Пропускать LLM-выход в Fusion без variance filter.** Один случайный сэмпл становится торговым сигналом, и нестабильность модели не видна.
- **Позволять LLM менять числовые поля после валидации.** Валидация теряет смысл: в исполнение уходит значение, которое не проверялось.
- **LLM возвращает «execute: true» или вызывает функцию отправки ордера.** Это нарушает механическое разделение: недетерминированный компонент получает право на необратимое действие.
- **При таймауте ждать или повторять до успеха.** Срывается TTL сигнала. Правильный путь — fallback в ML-only.