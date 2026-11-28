"""Схемы выхода LLM (ML_LLM_FUSION_SPEC.md §3.2; skill llm-output-validation).

Свободный текст — только `rationale`; он никогда не парсится кодом и не влияет на решение.
Каждый `risk_flag` ссылается на id из входа (grounding через `context={"input_ids": ...}`).
LLM не предлагает сделку: полей action/amount/execute в схемах нет.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

__all__ = [
    "LLMContext",
    "LLMContextResult",
    "NewsSentimentOut",
    "RiskFlag",
    "RiskFlagCode",
    "Severity",
    "SignalExplanation",
]


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskFlagCode(StrEnum):
    HIGH_IMPACT_EVENT_IMMINENT = "high_impact_event_imminent"
    NEWS_PRICE_DIVERGENCE = "news_price_divergence"
    REGULATORY_ACTION = "regulatory_action"
    EXCHANGE_INCIDENT = "exchange_incident"
    LIQUIDITY_SHOCK = "liquidity_shock"
    EARNINGS_WINDOW = "earnings_window"
    DEPEG_OR_DEFAULT = "depeg_or_default"
    DATA_QUALITY_SUSPECT = "data_quality_suspect"


class RiskFlag(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: RiskFlagCode
    severity: Severity
    evidence_ids: list[str] = Field(min_length=1, max_length=10)


class LLMContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0"]
    asset: str
    as_of: datetime
    context_score: float = Field(ge=-1.0, le=1.0)
    risk_flags: list[RiskFlag] = Field(default_factory=list, max_length=8)
    confidence: float = Field(ge=0.0, le=1.0)  # ординальная, не калиброванная
    rationale: str = Field(min_length=1, max_length=1200)

    @field_validator("risk_flags")
    @classmethod
    def _evidence_must_exist(cls, flags: list[RiskFlag], info: ValidationInfo) -> list[RiskFlag]:
        allowed: set[str] = (info.context or {}).get("input_ids", set())
        for f in flags:
            unknown = set(f.evidence_ids) - allowed
            if unknown:
                raise ValueError(f"ungrounded evidence {sorted(unknown)} for {f.code.value}")
        return flags


class LLMContextResult(BaseModel):
    """Итог `LLMContextService` (§3.4). `context=None` при любом статусе, кроме ok."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok", "rejected", "fallback_ml_only"]
    context: LLMContext | None
    llm_call_ids: list[int]
    reject_reason: str | None
    prompt_version: str
    context_id: str | None = None  # llm_contexts.id


class NewsSentimentOut(BaseModel):
    """Выход LLM-скорера новости (`prompt_news_v1`): только оценка, без действий."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0"]
    news_id: str
    sentiment: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    importance: Literal["low", "medium", "high"]
    rationale: str = Field(min_length=1, max_length=600)

    @field_validator("news_id")
    @classmethod
    def _grounded(cls, v: str, info: ValidationInfo) -> str:
        expected = (info.context or {}).get("news_id")
        if v != expected:
            raise ValueError(f"ungrounded news_id {v!r}")
        return v


class SignalExplanation(BaseModel):
    """Выход LLM `explain` (`prompt_explain_v1`, FUSION SPEC §3.1): текст только для аудита.

    Пишется в `decision_audit.explanation` и никогда не возвращается в цепочку решения.
    Полей действия/направления/объёма нет; `decision_id` должен совпасть с входом.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1.0"]
    decision_id: str
    summary: str = Field(min_length=1, max_length=800)
    key_factors: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=5
    )
    caveats: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=3
    )

    @field_validator("decision_id")
    @classmethod
    def _grounded(cls, v: str, info: ValidationInfo) -> str:
        expected = (info.context or {}).get("decision_id")
        if v != expected:
            raise ValueError(f"ungrounded decision_id {v!r}")
        return v
