"""`FusionClient` Protocol, `FusionRequest` и `StubFusionClient` (ARCHITECTURE.md §5.4).

`StubFusionClient` (0.4.0) не содержит ML/LLM: возвращает неисполняемый HOLD / NO_SIGNAL,
чтобы путь Signal Engine → Fusion → (Risk Gate, 0.5.0) был проверяем без реального контура.
"""

import uuid
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from augur import versions
from augur.data.models import timeframe_ms
from augur.fusion.contract import FusedSignal
from augur.strategies.base import SignalAction

__all__ = [
    "STUB_EXPLANATION_REF",
    "STUB_FUSION_CONFIG_VERSION",
    "STUB_NAMESPACE",
    "FusionClient",
    "FusionRequest",
    "StubFusionClient",
]

STUB_FUSION_CONFIG_VERSION = "stub-0.4.0"
STUB_EXPLANATION_REF = "fusion_stub"
# Пространство имён uuid5 для детерминированного signal_id заглушки.
STUB_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "augur:fusion:stub")


class FusionRequest(BaseModel):
    """Запрос к Fusion на один закрытый бар. `timestamp` — close_time бара, Unix ms UTC."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    asset: str = Field(min_length=1)
    timeframe: str
    timestamp: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)


class FusionClient(Protocol):
    """Граница с гибридным контуром. Реализация MUST быть идемпотентной по `idempotency_key`."""

    async def get_fused_signal(self, request: FusionRequest) -> FusedSignal | None:
        """Сигнал Fusion или None, если сигнала нет (Signal Engine уходит в fallback)."""
        ...


class StubFusionClient:
    """Заглушка Fusion: без ML/LLM, детерминированная по `idempotency_key`.

    - `HOLD`, если в `context` есть технический сигнал (`strategy_signal_id`), иначе `NO_SIGNAL`.
    - `strength=0`, `reason="fusion_stub"`, `explanation_ref="fusion_stub"`, `risk_flags=[]`.
    - `signal_id = uuid5(STUB_NAMESPACE, idempotency_key)`; TTL — половина бара
      (ML_LLM_FUSION_SPEC.md §4.2 `signal_ttl_fraction_of_bar`).
    """

    async def get_fused_signal(self, request: FusionRequest) -> FusedSignal | None:
        has_tech = request.context.get("strategy_signal_id") is not None
        return FusedSignal(
            api_version=versions.API_VERSION,
            signal_id=str(uuid.uuid5(STUB_NAMESPACE, request.idempotency_key)),
            idempotency_key=request.idempotency_key,
            asset=request.asset,
            timestamp=request.timestamp,
            expires_at=request.timestamp + max(1, timeframe_ms(request.timeframe) // 2),
            action=SignalAction.HOLD if has_tech else SignalAction.NO_SIGNAL,
            strength=0.0,
            reason="fusion_stub",
            explanation_ref=STUB_EXPLANATION_REF,
            risk_flags=[],
            source_breakdown={"ml": 0.0, "tech": 1.0, "llm": 0.0, "rules_fired": []},
            fusion_config_version=STUB_FUSION_CONFIG_VERSION,
        )
