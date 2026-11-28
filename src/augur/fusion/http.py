"""`HttpFusionClient` — клиент к внешнему Fusion-сервису `augur fusion serve` (ARCH §5.4; 0.12.0).

HTTP-контракт (версия = `api_version` контракта `fused_signal`, сейчас 1.0):

- `POST {endpoint}/v1/fuse` — тело `FusionRequest` (ARCH §5.2); заголовки
  `Authorization: Bearer <FUSION_API_TOKEN>`, `X-Augur-Api-Version: <API_VERSION>`.
  Ответы: 200 — `FusedSignal` (FUSION SPEC §4.6); 204 — сигнала нет; 401 — токен;
  409 — другой MAJOR `api_version`; 422 — невалидный запрос; 503 — сбой Fusion.
- `GET {endpoint}/v1/health` — `{"status", "api_version", "fusion_config_version", "llm"}`.

Клиент не повторяет запрос (FUSION SPEC skill п. 10): любой сбой — `FusionUnavailable`, Signal
Engine уходит в ML-disabled (`FUSION_FALLBACK`). Ответ 200 валидируется Pydantic-моделью
`FusedSignal` (`extra="forbid"`, `api_version`), затем Signal Engine проверяет V1–V5 повторно.
Токен не попадает в исключения и логи: в ошибке — только тип исключения или HTTP-код.
"""

from typing import Any

import httpx
from pydantic import SecretStr

from augur import versions
from augur.fusion.client import FusionRequest
from augur.fusion.contract import FusedSignal

__all__ = ["API_VERSION_HEADER", "FusionUnavailable", "HttpFusionClient"]

API_VERSION_HEADER = "X-Augur-Api-Version"


class FusionUnavailable(RuntimeError):
    """Fusion-сервис не ответил валидно (сеть, таймаут, HTTP ≠ 200/204)."""


class HttpFusionClient:
    def __init__(
        self,
        endpoint: str,
        *,
        token: SecretStr,
        timeout_ms: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token.get_secret_value():
            raise ValueError("FUSION_API_TOKEN is empty")
        self._base = endpoint.rstrip("/")
        self._token = token
        self._timeout = max(timeout_ms, 1) / 1000
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            API_VERSION_HEADER: versions.API_VERSION,
            "Content-Type": "application/json",
        }

    async def _send(self, method: str, path: str, content: str | None = None) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                base_url=self._base, timeout=self._timeout, transport=self._transport
            ) as c:
                return await c.request(method, path, content=content, headers=self._headers())
        except httpx.HTTPError as e:
            raise FusionUnavailable(f"fusion service unreachable: {type(e).__name__}") from None

    async def get_fused_signal(self, request: FusionRequest) -> FusedSignal | None:
        r = await self._send("POST", "/v1/fuse", request.model_dump_json())
        if r.status_code == 204:
            return None
        if r.status_code != 200:
            raise FusionUnavailable(f"fusion service HTTP {r.status_code}")
        return FusedSignal.model_validate_json(r.content)  # ValidationError → invalid (V1)

    async def health(self) -> dict[str, Any]:
        r = await self._send("GET", "/v1/health")
        if r.status_code != 200:
            raise FusionUnavailable(f"fusion service HTTP {r.status_code}")
        data: dict[str, Any] = r.json()
        return data

    def describe(self) -> dict[str, Any]:
        return {"endpoint": self._base}
