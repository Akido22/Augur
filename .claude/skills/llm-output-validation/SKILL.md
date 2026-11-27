---
name: llm-output-validation
description: Hallucination defense for every LLM output in Augur - strict Pydantic LLMContext schema (FUSION SPEC §3.2) with grounding of risk_flags by evidence_ids, reject-not-repair (only a re-ask with the validation error, never regex fixes), semantic checks, multi-sample variance filter, llm_calls logging, budget/latency/secret-scan fallback to ML-only. LLM never proposes a trade. Use when creating or modifying src/augur/fusion/llm/*, LLM clients, prompts, the LLM news SentimentScorer, or any code that parses, stores or consumes LLM output. Do NOT use for combining validated LLM context with ML signals (use hybrid-ml-llm-fusion) or for order execution (use auto-trade-risk-gate).
---

# llm-output-validation

Защита от галлюцинаций LLM. Невалидный выход отбрасывается и никогда не «чинится». LLM только оценивает контекст: он не предлагает сделку, направление и объём. Решает детерминированный код (`hybrid-ml-llm-fusion` → `auto-trade-risk-gate`).

## When to use
- Создаёшь или меняешь `src/augur/fusion/llm/*`: клиент, промпты, валидаторы, variance filter, LLM-скорер новостей.
- Пишешь код, который парсит, сохраняет в `llm_calls` или передаёт дальше выход LLM.
- Меняешь бюджеты, таймауты, число сэмплов или провайдеров LLM.

When NOT to use:
- Слияние уже валидного LLM-контекста с ML → `hybrid-ml-llm-fusion`.
- Ордера, лимиты, режимы → `auto-trade-risk-gate`.

## Procedure
1. Прочитай `ML_LLM_FUSION_SPEC.md` §3 (LLM-слой), §5 (верификация, variance filter), §4.4 (anti-sycophancy), §7 (`llm_calls`).
2. **Размещение.** Весь LLM-код — в `augur.fusion.llm`. Клиент — OpenAI-совместимый HTTP (`httpx`, `base_url` + `model` из конфига), без SDK провайдеров. LLM-код получает только LLM-настройки и ключи `LLM_*_KEY`, никогда не `Settings` с ключами биржи. Нет tool use / function calling.
3. **Схема выхода** — `LLMContext` по §3.2: `schema_version`, `asset`, `as_of`, `context_score ∈ [-1, 1]`, `risk_flags` (`code` из `RiskFlagCode`, `severity`, `evidence_ids` 1..10), `confidence ∈ [0, 1]`, `rationale` (1..1200). `ConfigDict(extra="forbid", frozen=True, strict=True)`. Поля «action», «amount», «execute» в схеме запрещены.
4. **Парсинг:** `LLMContext.model_validate_json(raw, context={"input_ids": ...})`. Grounding: каждый `evidence_id` есть во входе, иначе reject `ungrounded`. При `ValidationError` допустим только повторный запрос (`max_retries` из конфига, по умолчанию 1) с текстом ошибки и без исходного ответа. Никаких регулярок, `json_repair`, синонимов (`"ACQUIMULATE"` → reject).
5. **Семантика** (§5.1): `asset` и `as_of` совпадают с запросом; `high_impact_event_imminent` только при событии в календаре; пустой вход → `|context_score| ≤ cfg.empty_input_max_abs_score`.
6. **Variance filter** (§5.2): N сэмплов (из конфига) с разным порядком новостей. Меньше `min_valid` валидных → reject; `pstdev(context_score) > max_score_std` → reject; размах `> max_score_range` при смене знака → reject. Итог: медиана скора, `min(confidence)`, флаги большинства; `high`, найденный только у меньшинства, понижается до `medium`.
7. После валидации объект неизменяем (`frozen=True`). Числовые поля не перезаписываются ни LLM, ни повторным вызовом.
8. **Fallback** — `LLMContextResult(status="fallback_ml_only" | "rejected", context=None)`: превышение бюджета (`daily_usd`, вызовов на актив в час), таймаут, ошибка провайдера, срабатывание сканера секретов, отбраковка всех сэмплов. Цепочка: провайдер → следующий → ML-only. Не ждать и не повторять до успеха. Записать WARNING в `audit_log`.
9. **Логирование:** каждый вызов, повтор и сэмпл — строка в `llm_calls` (`prompt_hash`, provider, model, токены, latency, `cost_usd` по ценам из конфига, `template_id`, `request_group`, `sample_index`, `temperature`, `status` из CHECK §7, `error`, сырой `response_json`). Ключи в лог и в `error` не попадают.
10. **Промпт** — нейтральный шаблон по §3.3, версия `prompt_<назначение>_v<N>` (VERSIONING §2.3). В промпт не попадают ML-прогноз, технический сигнал, позиция, P&L. Текст новостей — недоверенные данные: в разделителях, обрезан, без управляющих символов. Перед отправкой — сканер секретов.
11. **Тесты:** невалидный JSON, лишнее поле, неизвестный `code`, ungrounded `evidence_id`, `context_score` вне диапазона, пустой `rationale`, несовпадение `asset`/`as_of`, высокая дисперсия, смена знака, таймаут, бюджет, сканер секретов. Каждый случай даёт отсутствие контекста, а не исправленное значение.

## Examples
```python
# src/augur/fusion/llm/schema.py — FUSION SPEC §3.2
class LLMContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["1.0"]
    asset: str
    as_of: datetime
    context_score: float = Field(ge=-1.0, le=1.0)
    risk_flags: list[RiskFlag] = Field(default_factory=list, max_length=8)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=1200)

def parse(raw: str, input_ids: set[str]) -> LLMContext | None:
    try:
        return LLMContext.model_validate_json(raw, context={"input_ids": input_ids})
    except ValidationError:
        return None                      # reject, never repair
```

## Anti-patterns
- **Чинить невалидный JSON регуляркой вместо отбраковки.** Система выдумывает ответ, которого модель не давала, а аудит записывает его как решение LLM.
- **LLM предлагает BUY/SELL или объём.** Нарушает асимметрию полномочий (§0): LLM может только ослабить или заблокировать вход.
- **Флаг без `evidence_ids` из входа.** Выдуманное событие начинает блокировать или ослаблять сделки.
- **Пропускать LLM-выход в Fusion без variance filter.** Один случайный сэмпл становится частью решения.
- **ML-прогноз или позиция в промпте.** Модель подтверждает «желаемый» ответ (sycophancy).
- **При таймауте ждать или повторять до успеха.** Срывается TTL сигнала. Правильный путь — ML-only.
