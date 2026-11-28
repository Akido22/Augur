"""`augur fusion serve` (FastAPI) + `HttpFusionClient` (0.12.0; ARCH §5.4–5.5, FUSION SPEC §4.6)."""

import asyncio
import sqlite3
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from augur import versions
from augur.config.settings import FusionSettings
from augur.fusion.client import FusionRequest, StubFusionClient
from augur.fusion.contract import FusedSignal
from augur.fusion.http import API_VERSION_HEADER, FusionUnavailable, HttpFusionClient
from augur.fusion.server import create_fusion_app
from tests.conftest import make_settings

TOKEN = SecretStr("fusion-test-token-0123456789")
AUTH = {"Authorization": f"Bearer {TOKEN.get_secret_value()}"}
REQ = FusionRequest(
    asset="BTC/USDT",
    timeframe="1h",
    timestamp=1_795_161_600_000,
    idempotency_key="k" * 64,
    context={"strategy_signal_id": "s1", "decision_audit_id": "01JD3XB4K7M2N8P9Q0R1S2T3V4"},
)


class CountingClient:
    def __init__(self, result: str = "stub") -> None:
        self.calls = 0
        self.result = result

    async def get_fused_signal(self, request: FusionRequest) -> FusedSignal | None:
        self.calls += 1
        if self.result == "none":
            return None
        if self.result == "boom":
            raise RuntimeError("secret detail must not leak")
        return await StubFusionClient().get_fused_signal(request)


def _app(client: Any, jobs: Any = None) -> Any:
    return create_fusion_app(
        token=TOKEN,
        connect=lambda: sqlite3.connect(":memory:"),
        build_client=lambda _c: client,
        build_jobs=jobs,
        jobs_interval_s=0.01,
        describe={"fusion_config_version": "fusion-2026.11.1", "llm": "disabled"},
    )


def test_health_and_auth() -> None:
    with TestClient(_app(CountingClient())) as tc:
        assert tc.get("/v1/health").status_code == 401
        assert tc.get("/v1/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
        r = tc.get("/v1/health", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["api_version"] == versions.API_VERSION
        assert tc.get("/docs").status_code == 404 and tc.get("/openapi.json").status_code == 404


def test_fuse_200_idempotent_204_409_422_503() -> None:
    c = CountingClient()
    with TestClient(_app(c)) as tc:
        body = REQ.model_dump_json()
        r1 = tc.post("/v1/fuse", content=body, headers=AUTH)
        r2 = tc.post("/v1/fuse", content=body, headers=AUTH)
        assert r1.status_code == 200 and r1.json() == r2.json() and c.calls == 1
        sig = FusedSignal.model_validate(r1.json())
        assert sig.idempotency_key == REQ.idempotency_key and sig.api_version == "1.0"
        assert tc.post("/v1/fuse", content=body).status_code == 401
        bad_ver = {**AUTH, API_VERSION_HEADER: "2.0"}
        assert tc.post("/v1/fuse", content=body, headers=bad_ver).status_code == 409
        ok_minor = {**AUTH, API_VERSION_HEADER: "1.7"}
        assert tc.post("/v1/fuse", content=body, headers=ok_minor).status_code == 200
        assert tc.post("/v1/fuse", content='{"asset": ""}', headers=AUTH).status_code == 422
        assert tc.post("/v1/fuse", content="not json", headers=AUTH).status_code == 422
    with TestClient(_app(CountingClient("none"))) as tc:
        assert tc.post("/v1/fuse", content=REQ.model_dump_json(), headers=AUTH).status_code == 204
    with TestClient(_app(CountingClient("boom"))) as tc:
        r = tc.post("/v1/fuse", content=REQ.model_dump_json(), headers=AUTH)
        assert r.status_code == 503 and "secret" not in r.text


def test_background_jobs_run_on_own_connection() -> None:
    ticks: list[sqlite3.Connection] = []

    class Jobs:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        async def tick(self) -> None:
            ticks.append(self.conn)

    app = _app(CountingClient(), jobs=Jobs)
    with TestClient(app) as tc:
        tc.post("/v1/fuse", content=REQ.model_dump_json(), headers=AUTH)
        for _ in range(50):
            if ticks:
                break
            asyncio.run(asyncio.sleep(0.01))
        assert ticks and ticks[0] is not app.state.conn


def _via(tc: TestClient) -> httpx.MockTransport:
    """httpx-транспорт клиента → ASGI-приложение в TestClient (с lifespan)."""

    def handler(req: httpx.Request) -> httpx.Response:
        r = tc.request(req.method, req.url.path, content=req.content, headers=dict(req.headers))
        return httpx.Response(r.status_code, content=r.content, headers=r.headers)

    return httpx.MockTransport(handler)


def test_http_client_roundtrip() -> None:
    with TestClient(_app(CountingClient())) as tc:
        client = HttpFusionClient(
            "http://fusion.local:8100/", token=TOKEN, timeout_ms=1500, transport=_via(tc)
        )
        sig = asyncio.run(client.get_fused_signal(REQ))
        local = asyncio.run(StubFusionClient().get_fused_signal(REQ))
        assert sig == local
        assert asyncio.run(client.health())["status"] == "ok"
    with TestClient(_app(CountingClient("none"))) as tc:
        client = HttpFusionClient(
            "http://fusion.local", token=TOKEN, timeout_ms=1500, transport=_via(tc)
        )
        assert asyncio.run(client.get_fused_signal(REQ)) is None


@pytest.mark.parametrize("status", [401, 409, 500, 503])
def test_http_client_errors_are_unavailable(status: int) -> None:
    t = httpx.MockTransport(lambda _r: httpx.Response(status))
    client = HttpFusionClient("http://f", token=TOKEN, timeout_ms=100, transport=t)
    with pytest.raises(FusionUnavailable, match=str(status)):
        asyncio.run(client.get_fused_signal(REQ))


def test_http_client_network_error_hides_token() -> None:
    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout(f"timeout {TOKEN.get_secret_value()}")

    client = HttpFusionClient(
        "http://f", token=TOKEN, timeout_ms=100, transport=httpx.MockTransport(boom)
    )
    with pytest.raises(FusionUnavailable) as ei:
        asyncio.run(client.get_fused_signal(REQ))
    assert TOKEN.get_secret_value() not in str(ei.value) and ei.value.__cause__ is None


def test_http_client_rejects_invalid_signal() -> None:
    sig = asyncio.run(StubFusionClient().get_fused_signal(REQ))
    assert sig is not None
    bad = sig.model_dump(mode="json") | {"api_version": "2.0"}
    extra = sig.model_dump(mode="json") | {"action_size": 1}
    for body in (bad, extra):
        t = httpx.MockTransport(lambda _r, b=body: httpx.Response(200, json=b))
        client = HttpFusionClient("http://f", token=TOKEN, timeout_ms=100, transport=t)
        with pytest.raises(ValidationError):
            asyncio.run(client.get_fused_signal(REQ))


def test_factory_builds_http_client(db: sqlite3.Connection) -> None:
    from augur.fusion.factory import build_fusion_client

    s = make_settings(
        fusion=FusionSettings(enabled=True, endpoint="http://127.0.0.1:8100"),
        fusion_api_token=TOKEN,
    )
    assert isinstance(build_fusion_client(db, s, clock=lambda: 0), HttpFusionClient)
    no_token = make_settings(fusion=FusionSettings(enabled=True, endpoint="http://127.0.0.1:8100"))
    with pytest.raises(ValueError, match="FUSION_API_TOKEN"):
        build_fusion_client(db, no_token, clock=lambda: 0)
    with pytest.raises(ValidationError):
        FusionSettings(endpoint="ftp://x")


async def test_signal_engine_falls_back_when_service_down(db: sqlite3.Connection) -> None:
    """Сервис недоступен → FUSION_FALLBACK (ML-disabled), без исключения наружу (ARCH §5.6)."""
    from augur.signals import SignalEngine
    from tests.test_signal_engine import _audit_rows, _sma_signals

    t = httpx.MockTransport(lambda _r: httpx.Response(503))
    client = HttpFusionClient("http://f", token=TOKEN, timeout_ms=100, transport=t)
    signals = _sma_signals()[:1]
    engine = SignalEngine(client, audit_conn=db, clock=lambda: signals[0].timestamp)
    assert await engine.process(signals) == []
    rows = _audit_rows(db)
    assert [r[0] for r in rows] == ["FUSION_FALLBACK"]
    assert TOKEN.get_secret_value() not in str(rows)
