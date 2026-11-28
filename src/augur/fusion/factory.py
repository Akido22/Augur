"""Сборка `FusionClient` из настроек (composition root Fusion, ARCH §5.4).

- `fusion.enabled=false` → `StubFusionClient` (Signal Engine всё равно идёт по классике).
- `enabled=true`, `endpoint` задан → внешний сервис (`HttpFusionClient` пока не реализован).
- `enabled=true` без `endpoint` → `LocalFusionClient` (0.10.0).
LLM-компонентам передаются только LLM-настройки и ключи `LLM_*_KEY` (FUSION SPEC §3.6):
`Settings` с ключами биржи дальше этой функции не уходит.
"""

import sqlite3
from collections.abc import Callable, Mapping

from pydantic import SecretStr

from augur.config.settings import Settings
from augur.fusion.client import FusionClient, StubFusionClient
from augur.fusion.llm.client import LLMClient, OpenAICompatClient
from augur.fusion.llm.service import LLMContextService
from augur.fusion.local import LocalFusionClient
from augur.fusion.ml.inference import MLService
from augur.fusion.ml.registry import ModelRegistry

__all__ = ["build_fusion_client", "build_llm_context_service", "llm_keys"]


def llm_keys(settings: Settings) -> Mapping[str, SecretStr | None]:
    return {
        "anthropic": settings.llm_anthropic_key,
        "deepseek": settings.llm_deepseek_key,
        "openai": settings.llm_openai_key,
    }


def build_llm_context_service(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    clock: Callable[[], int],
    client: LLMClient | None = None,
) -> LLMContextService:
    return LLMContextService(
        conn,
        settings.fusion.llm,
        client or OpenAICompatClient(),
        llm_keys(settings),
        clock=clock,
    )


def build_fusion_client(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    clock: Callable[[], int],
    llm_client: LLMClient | None = None,
) -> FusionClient:
    fcfg = settings.fusion
    if not fcfg.enabled:
        return StubFusionClient()
    if fcfg.endpoint is not None:
        raise NotImplementedError("HttpFusionClient is not implemented; unset FUSION__ENDPOINT")
    registry = ModelRegistry(conn, clock=clock)
    ml = MLService(conn, registry, fcfg.ml, clock=clock)
    llm = (
        build_llm_context_service(conn, settings, clock=clock, client=llm_client)
        if fcfg.llm.mode == "enabled"
        else None
    )
    return LocalFusionClient(conn, fcfg, ml=ml, llm=llm, clock=clock)
