"""Fusion-контракт и клиенты (ML_LLM_FUSION_SPEC.md §4.6, ARCHITECTURE.md §5).

0.4.0: контракт и `StubFusionClient`. 0.10.0: реальный контур — `augur.fusion.ml` (ML),
`augur.fusion.llm` (LLM), `augur.fusion.engine.fuse()`, `LocalFusionClient`
(`augur.fusion.local`), сборка — `augur.fusion.factory.build_fusion_client`. Эти подмодули
импортируются явно: пакет `augur.fusion` не тянет numpy/lightgbm/httpx при импорте контракта.
"""

from augur.fusion.client import FusionClient, FusionRequest, StubFusionClient
from augur.fusion.contract import FusedSignal, RiskFlag

__all__ = ["FusedSignal", "FusionClient", "FusionRequest", "RiskFlag", "StubFusionClient"]
