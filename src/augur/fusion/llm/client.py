"""OpenAI-совместимый LLM-клиент на httpx (ML_LLM_FUSION_SPEC.md §3.4, §3.6).

Один клиент для всех провайдеров (`base_url` + `model`). Нет tool use / function calling.
Запросы — только к `base_url` из конфига. Ключ уходит только в заголовок `Authorization` и
никогда не попадает в текст ошибок и логи.
"""

import time
from typing import Any, Literal, Protocol

import httpx
from pydantic import SecretStr

from augur.config.settings import LLMProvider

__all__ = ["LLMClient", "LLMHTTPError", "OpenAICompatClient", "RawCompletion"]

ErrorKind = Literal["timeout", "http_5xx", "rate_limit", "http_error"]


class RawCompletion:
    def __init__(self, text: str, input_tokens: int, output_tokens: int, latency_ms: int) -> None:
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms


class LLMHTTPError(Exception):
    def __init__(self, kind: ErrorKind, message: str, latency_ms: int) -> None:
        super().__init__(message)
        self.kind = kind
        self.latency_ms = latency_ms


class LLMClient(Protocol):
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
    ) -> RawCompletion: ...


class OpenAICompatClient:
    """`POST {base_url}/chat/completions` с `response_format={"type": "json_object"}`."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

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
        url = provider.base_url.rstrip("/") + "/chat/completions"
        body: dict[str, Any] = {
            "model": provider.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if temperature is not None and provider.send_temperature:
            body["temperature"] = temperature
        headers = {"Content-Type": "application/json"}
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key.get_secret_value()}"
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=timeout_s, transport=self._transport, follow_redirects=False
            ) as http:
                resp = await http.post(url, json=body, headers=headers)
        except httpx.TimeoutException:
            raise LLMHTTPError("timeout", "request timed out", _ms(started)) from None
        except httpx.HTTPError as e:
            raise LLMHTTPError("http_error", type(e).__name__, _ms(started)) from None
        latency = _ms(started)
        if resp.status_code == 429:
            raise LLMHTTPError("rate_limit", "HTTP 429", latency)
        if resp.status_code >= 500:
            raise LLMHTTPError("http_5xx", f"HTTP {resp.status_code}", latency)
        if resp.status_code >= 400:
            raise LLMHTTPError("http_error", f"HTTP {resp.status_code}", latency)
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMHTTPError("http_error", "malformed provider response", latency) from None
        if not isinstance(text, str):
            raise LLMHTTPError("http_error", "empty completion", latency)
        return RawCompletion(
            text=text,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=latency,
        )


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
