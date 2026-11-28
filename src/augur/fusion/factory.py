"""Сборка `FusionClient` из настроек (composition root Fusion, ARCH §5.4).

- `fusion.enabled=false` → `StubFusionClient` (Signal Engine всё равно идёт по классике).
- `enabled=true`, `endpoint` задан → внешний сервис (`HttpFusionClient` — 0.12.0).
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
from augur.fusion.llm.explain import ExplainService
from augur.fusion.llm.inputs import load_news_inputs, load_price_summary
from augur.fusion.llm.schema import LLMContextResult
from augur.fusion.llm.service import LLMContextService
from augur.fusion.local import LocalFusionClient
from augur.fusion.ml.inference import MLService
from augur.fusion.ml.registry import ModelRegistry

__all__ = [
    "build_explain_service",
    "build_fusion_client",
    "build_llm_context_service",
    "llm_keys",
    "refresh_llm_context",
]


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


def build_explain_service(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    clock: Callable[[], int],
    client: LLMClient | None = None,
) -> ExplainService:
    """LLM `explain` (0.11.0): только LLM-настройки и ключи `LLM_*_KEY`."""
    return ExplainService(
        conn, settings.fusion.llm, client or OpenAICompatClient(), llm_keys(settings), clock=clock
    )


async def refresh_llm_context(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    symbol: str,
    timeframe: str,
    clock: Callable[[], int],
    event_driven: bool = False,
    client: LLMClient | None = None,
) -> LLMContextResult:
    """Один цикл LLM-контекста по активу: новости + сводка цены → `llm_contexts`.

    Общий для `augur llm refresh` и оркестратора (`augur run`).
    """
    lcfg = settings.fusion.llm
    now = clock()
    news = load_news_inputs(
        conn,
        asset=symbol,
        as_of=now,
        window_h=lcfg.news_window_h,
        limit=lcfg.max_news,
        body_chars=lcfg.news_body_chars,
    )
    price = load_price_summary(
        conn,
        venue=settings.fusion.ml.venue,
        asset=symbol,
        timeframe=timeframe,
        as_of=now,
        window_h=lcfg.news_window_h,
    )
    svc = build_llm_context_service(conn, settings, clock=clock, client=client)
    return await svc.refresh(symbol, as_of=now, news=news, price=price, event_driven=event_driven)


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
