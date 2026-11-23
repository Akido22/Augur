"""Контракт `fused_signal` (ML_LLM_FUSION_SPEC.md §4.6 — источник истины).

Модель описывает сигнал на границе `FusionClient` → Signal Engine, то есть уже после
маппинга внутреннего Fusion-`Action` в `SignalAction` (ARCHITECTURE.md §2.7).

Структурная валидация (reject, never repair): неизвестные поля, `action` вне 6 значений,
`strength` вне [0, 1], пустой `explanation_ref`, чужой `api_version`, `expires_at <= timestamp`
дают `ValidationError` — такой сигнал считается отсутствующим. Проверку TTL относительно
текущего времени делает потребитель (Signal Engine), а не модель: в модели нет часов.
"""

import uuid
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from augur import versions
from augur.strategies.base import SignalAction

__all__ = ["FusedSignal", "RiskFlag", "SourceBreakdownValue"]

# {ml, tech, llm} — числа (0.4.0) или объекты трассы (0.10.0+), rules_fired — список строк.
SourceBreakdownValue = float | list[str] | dict[str, Any]


class RiskFlag(BaseModel):
    """Флаг риска из Fusion. Может только ужесточать решение Risk Gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    severity: Literal["low", "medium", "high"]


class FusedSignal(BaseModel):
    """Выход Fusion (ML_LLM_FUSION_SPEC.md §4.6). Время — Unix ms UTC."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_version: str
    signal_id: str  # UUID, выданный Fusion
    idempotency_key: str = Field(min_length=1)
    asset: str = Field(min_length=1)
    timestamp: int = Field(ge=0)
    expires_at: int = Field(ge=0)
    action: SignalAction
    strength: float = Field(ge=0.0, le=1.0)
    reason: str
    # Строка-ссылка, НЕ FK: "fusion_stub" (0.4.0) | "decision_audit:<ulid>" (0.5.0+).
    explanation_ref: str
    risk_flags: list[RiskFlag]
    source_breakdown: dict[str, SourceBreakdownValue]
    fusion_config_version: str = Field(min_length=1)

    @field_validator("api_version")
    @classmethod
    def _api_version_matches(cls, v: str) -> str:
        if v != versions.API_VERSION:
            raise ValueError(f"api_version mismatch: got {v}, expected {versions.API_VERSION}")
        return v

    @field_validator("explanation_ref")
    @classmethod
    def _explanation_ref_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("explanation_ref must not be empty")
        return v

    @field_validator("signal_id")
    @classmethod
    def _signal_id_is_uuid(cls, v: str) -> str:
        try:
            uuid.UUID(v)
        except ValueError as e:
            raise ValueError(f"signal_id must be a UUID, got {v!r}") from e
        return v

    @model_validator(mode="after")
    def _expires_after_timestamp(self) -> Self:
        if self.expires_at <= self.timestamp:
            raise ValueError(
                f"expires_at ({self.expires_at}) must be > timestamp ({self.timestamp})"
            )
        return self
