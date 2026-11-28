"""`augur fusion serve` — Fusion как отдельный HTTP-сервис (ARCH §5.4; FUSION SPEC §3.6; 0.12.0).

FastAPI над in-process `LocalFusionClient` (тот же `fuse()`, ML-реестр и `llm_contexts`, что и
в 0.10.0) + фоновые задачи `FusionJobs` (LLM-контекст, explain, LLM-breaker, nightly/PSI).
Контракт ответа — `FusedSignal` (FUSION SPEC §4.6, `api_version` 1.0) без изменений;
HTTP-контракт — `augur.fusion.http`.

- Аутентификация — `Authorization: Bearer <FUSION_API_TOKEN>` (сравнение `compare_digest`);
  токен не логируется.
- Идемпотентность по `idempotency_key`: повтор того же запроса возвращает тот же ответ из
  LRU-кэша процесса (повтор после рестарта даёт тот же `signal_id` — он детерминирован).
- Запросы обрабатываются последовательно (одно соединение SQLite, детерминированный порядок);
  фоновые задачи работают на своём соединении и не задерживают `/v1/fuse`.
- Сбой Fusion → 503 без деталей: клиент уходит в ML-disabled, сервис продолжает работать.
"""

import asyncio
import logging
import secrets
import sqlite3
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import SecretStr

from augur import versions
from augur.fusion.client import FusionClient, FusionRequest
from augur.fusion.contract import FusedSignal
from augur.fusion.http import API_VERSION_HEADER

__all__ = ["create_fusion_app"]

log = logging.getLogger(__name__)
_CACHE_SIZE = 1024


def _major(v: str) -> str:
    return v.split(".", 1)[0]


def create_fusion_app(
    *,
    token: SecretStr,
    connect: Callable[[], sqlite3.Connection],
    build_client: Callable[[sqlite3.Connection], FusionClient],
    build_jobs: Callable[[sqlite3.Connection], Any] | None = None,
    jobs_interval_s: float = 5.0,
    describe: dict[str, Any] | None = None,
) -> FastAPI:
    """Args:
    token: `FUSION_API_TOKEN`.
    connect: открывает соединение SQLite (вызывается в lifespan, в потоке event loop).
    build_client: `LocalFusionClient` над соединением (composition root — CLI).
    build_jobs: `FusionJobs` над соединением; None — без фоновых задач.
    jobs_interval_s: период планировщика фоновых задач.
    describe: поля `/v1/health` (fusion_config_version, llm).
    """
    if not token.get_secret_value():
        raise ValueError("FUSION_API_TOKEN is empty")
    expected = f"Bearer {token.get_secret_value()}".encode()
    info = {"status": "ok", "api_version": versions.API_VERSION, **(describe or {})}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        conn = connect()
        app.state.conn = conn
        app.state.client = build_client(conn)
        app.state.lock = asyncio.Lock()
        app.state.cache = OrderedDict()
        task: asyncio.Task[None] | None = None
        jobs_conn: sqlite3.Connection | None = None
        if build_jobs is not None:
            # Отдельное соединение: LLM-вызовы задач (секунды) не держат запросы /v1/fuse,
            # а их транзакции не смешиваются (SQLite WAL).
            jobs_conn = connect()
            jobs = build_jobs(jobs_conn)

            async def loop() -> None:
                while True:
                    await jobs.tick()
                    await asyncio.sleep(jobs_interval_s)

            task = asyncio.create_task(loop())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if jobs_conn is not None:
                jobs_conn.close()
            conn.close()

    app = FastAPI(
        title="Augur Fusion",
        version=versions.API_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def _auth(authorization: str | None) -> None:
        if authorization is None or not secrets.compare_digest(authorization.encode(), expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/v1/health")
    async def health(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        return info

    @app.post("/v1/fuse", response_model=None)
    async def fuse(
        request: Request,
        authorization: str | None = Header(default=None),
        api_version: str | None = Header(default=None, alias=API_VERSION_HEADER),
    ) -> Response:
        _auth(authorization)
        if api_version is not None and _major(api_version) != _major(versions.API_VERSION):
            raise HTTPException(status_code=409, detail="api_version mismatch")
        try:
            req = FusionRequest.model_validate_json(await request.body())
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid FusionRequest") from None
        state = request.app.state
        async with state.lock:
            cache: OrderedDict[str, FusedSignal | None] = state.cache
            if req.idempotency_key in cache:
                sig = cache[req.idempotency_key]
                cache.move_to_end(req.idempotency_key)
            else:
                try:
                    sig = await state.client.get_fused_signal(req)
                except Exception as e:  # noqa: BLE001 — сбой Fusion → 503, сервис живёт
                    log.warning("fusion failed for %s: %s", req.asset, type(e).__name__)
                    try:
                        state.conn.rollback()
                    except sqlite3.Error:
                        pass
                    raise HTTPException(status_code=503, detail="fusion failed") from None
                cache[req.idempotency_key] = sig
                while len(cache) > _CACHE_SIZE:
                    cache.popitem(last=False)
        if sig is None:
            return Response(status_code=204)
        return JSONResponse(sig.model_dump(mode="json"))

    return app
