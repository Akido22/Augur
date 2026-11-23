"""Fusion-контракт и клиенты (ML_LLM_FUSION_SPEC.md §4.6, ARCHITECTURE.md §5).

0.4.0: только контракт и `StubFusionClient`; реальный ML/LLM — этап 0.10.0.
"""

from augur.fusion.client import FusionClient, FusionRequest, StubFusionClient
from augur.fusion.contract import FusedSignal, RiskFlag

__all__ = ["FusedSignal", "FusionClient", "FusionRequest", "RiskFlag", "StubFusionClient"]
