"""Фейковый LLM-клиент: очередь ответов или исключений по провайдеру; запись запросов."""

import json
from collections.abc import Callable
from typing import Any

from pydantic import SecretStr

from augur.config.settings import LLMProvider
from augur.fusion.llm.client import LLMHTTPError, RawCompletion

Reply = str | LLMHTTPError | Callable[[str, str], str]


class FakeLLM:
    def __init__(self, replies: dict[str, list[Reply]] | list[Reply]) -> None:
        self.replies = replies if isinstance(replies, dict) else {"*": replies}
        self.calls: list[dict[str, Any]] = []

    async def complete_json(
        self,
        provider: LLMProvider,
        *,
        api_key: SecretStr | None,
        system: str,
        user: str,
        temperature: float | None,
        timeout_s: float,
        max_tokens: int,
    ) -> RawCompletion:
        self.calls.append(
            {
                "provider": provider.name,
                "system": system,
                "user": user,
                "temperature": temperature,
                "key": api_key,
            }
        )
        queue = self.replies.get(provider.name) or self.replies.get("*") or []
        r = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(r, LLMHTTPError):
            raise r
        text = r(system, user) if callable(r) else r
        return RawCompletion(text, input_tokens=1000, output_tokens=200, latency_ms=5)


def ctx_json(
    score: float = 0.2,
    conf: float = 0.7,
    flags: list[dict[str, Any]] | None = None,
    asset: str = "BTC/USDT",
    as_of: str = "2026-11-20T08:00:00Z",
    **extra: Any,
) -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "asset": asset,
            "as_of": as_of,
            "context_score": score,
            "risk_flags": flags or [],
            "confidence": conf,
            "rationale": "фон умеренный",
            **extra,
        }
    )
