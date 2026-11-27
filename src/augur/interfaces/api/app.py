"""FastAPI: REST `/api/v1`, WebSocket `/ws/events`, GUI мониторинга (ARCHITECTURE.md §9.1).

Решения 0.9.0:
- GUI и REST только читают; единственная команда — `POST /api/v1/kill-switch` (ARCH §7.4).
  Approve первого ордера, смена режима, снятие паузы новостей — только CLI.
- Чтение идёт через соединение SQLite `mode=ro`: запись невозможна на уровне БД.
- Все `/api/v1/*` требуют `Authorization: Bearer <API_AUTH_TOKEN>`. WebSocket принимает токен
  первым сообщением `{"type": "auth", "token": ...}`, не в URL (URL попадают в логи).
- HTML-страницы данных не содержат: данные JS берёт из REST с токеном из sessionStorage.
- Оркестратора нет, поэтому `/ws/events` опрашивает БД (`EventTail`).
"""

import asyncio
import contextlib
import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.websockets import WebSocketDisconnect

from augur import __version__
from augur.adapters.base import MarketAdapter
from augur.config.settings import Settings
from augur.interfaces.api.events import EventTail
from augur.interfaces.control import engage_kill_switch
from augur.interfaces.dto import (
    AuditChainReportDTO,
    AuditEventDTO,
    DecisionChainDTO,
    KillSwitchRequestDTO,
    KillSwitchResultDTO,
    NewsDTO,
    NewsReactionDTO,
    OrderDTO,
    PortfolioSnapshotDTO,
    PositionDTO,
    SignalDTO,
    StatusDTO,
)
from augur.interfaces.queries import ChainNotFoundError, ReadService, verify_audit_chain
from augur.versions import DB_SCHEMA_VERSION

__all__ = ["create_app", "token_id"]

GUI_DIR = Path(__file__).resolve().parent.parent / "gui"
WS_AUTH_TIMEOUT_S = 5.0
WS_CLOSE_UNAUTHORIZED = 4401

PAGES: dict[str, str] = {
    "/": "dashboard.html",
    "/positions": "positions.html",
    "/signals": "signals.html",
    "/news": "news.html",
    "/control": "control.html",
}


def token_id(token: str) -> str:
    """Короткий отпечаток токена для `actor` в аудите (`api:<id>`). Сам токен не пишется."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def create_app(
    settings: Settings,
    *,
    db_path: str | None = None,
    clock: Callable[[], int] | None = None,
    ws_poll_s: float = 2.0,
    live_adapter: Callable[[Settings], MarketAdapter | None] | None = None,
) -> FastAPI:
    """Собирает приложение.

    Args:
        settings: загруженные настройки (нужен `api_auth_token`).
        db_path: путь к SQLite; по умолчанию `settings.db_path`.
        clock: «сейчас», Unix ms UTC.
        ws_poll_s: период опроса БД для `/ws/events`, секунды.
        live_adapter: фабрика реального адаптера для kill switch в MICRO_LIVE (тесты).
    """
    path = Path(db_path or settings.db_path)
    now = clock or (lambda: int(time.time() * 1000))
    expected = settings.api_auth_token.get_secret_value()
    if not expected:
        raise ValueError("API_AUTH_TOKEN is empty")

    app = FastAPI(
        title="Augur",
        version=__version__,
        docs_url=None,  # Swagger тянет JS с CDN — в GUI без внешних ресурсов не нужен
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
    )
    templates = Jinja2Templates(directory=str(GUI_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(GUI_DIR / "static")), name="static")
    bearer = HTTPBearer(auto_error=False)

    def _check_schema(conn: sqlite3.Connection) -> None:
        try:
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        except sqlite3.Error:
            row = None
        if row is None or row[0] is None or int(row[0]) < DB_SCHEMA_VERSION:
            conn.close()
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"database schema is older than {DB_SCHEMA_VERSION} (run: augur db migrate)",
            )

    def _connect_ro() -> sqlite3.Connection:
        if not path.is_file():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database not found")
        # Соединение на запрос. Sync-зависимость и sync-эндпоинт FastAPI выполняются в разных
        # потоках threadpool, поэтому check_same_thread=False; доступ к нему последовательный.
        conn = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False
        )
        conn.execute("PRAGMA busy_timeout=5000")
        _check_schema(conn)
        return conn

    def _connect_rw() -> sqlite3.Connection:
        if not path.is_file():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database not found")
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _check_schema(conn)
        return conn

    def require_token(
        creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        if creds is None or not secrets.compare_digest(
            creds.credentials.encode("utf-8"), expected.encode("utf-8")
        ):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "invalid or missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return f"api:{token_id(creds.credentials)}"

    def read_service() -> Iterator[ReadService]:
        conn = _connect_ro()
        try:
            yield ReadService(conn, settings=settings, clock=now)
        finally:
            conn.close()

    Actor = Annotated[str, Depends(require_token)]
    Svc = Annotated[ReadService, Depends(read_service)]
    Limit = Annotated[int, Query(ge=1, le=1000)]

    # --- REST: чтение ---------------------------------------------------------------

    @app.get("/api/v1/status", response_model=StatusDTO)
    def get_status(_: Actor, svc: Svc) -> StatusDTO:
        return svc.status()

    @app.get("/api/v1/signals", response_model=list[SignalDTO])
    def get_signals(
        _: Actor,
        svc: Svc,
        symbol: str | None = None,
        since: int | None = None,
        kind: str | None = Query(None, pattern="^(strategy|final)$"),
        limit: Limit = 50,
    ) -> list[SignalDTO]:
        return svc.signals(symbol=symbol, since=since, kind=kind, limit=limit)

    @app.get("/api/v1/signals/{ref_id}/chain", response_model=DecisionChainDTO)
    def get_signal_chain(ref_id: str, _: Actor, svc: Svc) -> DecisionChainDTO:
        try:
            return svc.decision_chain(ref_id)
        except ChainNotFoundError as e:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "signal or order not found") from e

    @app.get("/api/v1/orders/{order_id}/chain", response_model=DecisionChainDTO)
    def get_order_chain(order_id: str, _: Actor, svc: Svc) -> DecisionChainDTO:
        return get_signal_chain(order_id, _, svc)

    @app.get("/api/v1/orders", response_model=list[OrderDTO])
    def get_orders(
        _: Actor,
        svc: Svc,
        state: str | None = None,
        symbol: str | None = None,
        limit: Limit = 50,
    ) -> list[OrderDTO]:
        return svc.orders(state=state, symbol=symbol, limit=limit)

    @app.get("/api/v1/positions", response_model=list[PositionDTO])
    def get_positions(_: Actor, svc: Svc) -> list[PositionDTO]:
        return svc.positions()

    @app.get("/api/v1/portfolio/snapshots", response_model=list[PortfolioSnapshotDTO])
    def get_snapshots(
        _: Actor,
        svc: Svc,
        mode: str | None = None,
        since: int | None = None,
        limit: Annotated[int, Query(ge=1, le=10000)] = 500,
    ) -> list[PortfolioSnapshotDTO]:
        return svc.snapshots(mode=mode, since=since, limit=limit)

    @app.get("/api/v1/news", response_model=list[NewsDTO])
    def get_news(_: Actor, svc: Svc, symbol: str | None = None, limit: Limit = 50) -> list[NewsDTO]:
        return svc.news(symbol=symbol, limit=limit)

    @app.get("/api/v1/news/reactions", response_model=list[NewsReactionDTO])
    def get_reactions(_: Actor, svc: Svc) -> list[NewsReactionDTO]:
        return svc.news_reactions()

    @app.get("/api/v1/audit", response_model=list[AuditEventDTO])
    def get_audit(_: Actor, svc: Svc, limit: Limit = 50) -> list[AuditEventDTO]:
        return svc.recent_audit(limit=limit)

    @app.get("/api/v1/audit/verify-chain", response_model=AuditChainReportDTO)
    def get_verify_chain(_: Actor) -> AuditChainReportDTO:
        conn = _connect_ro()
        try:
            return verify_audit_chain(conn)
        finally:
            conn.close()

    # --- REST: единственная команда -------------------------------------------------

    @app.post("/api/v1/kill-switch", response_model=KillSwitchResultDTO)
    async def post_kill_switch(actor: Actor, body: KillSwitchRequestDTO) -> KillSwitchResultDTO:
        """Kill switch без кода (ARCH §7.4). Снятие — только `augur cb reset --confirm`."""
        conn = _connect_rw()
        try:
            adapter = live_adapter(settings) if live_adapter is not None else None
            return await engage_kill_switch(
                conn,
                settings,
                actor=actor,
                flatten=body.flatten,
                clock=now,
                live_adapter=adapter,
            )
        finally:
            conn.close()

    # --- WebSocket ------------------------------------------------------------------

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket) -> None:
        await ws.accept()
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=WS_AUTH_TIMEOUT_S)
            msg: Any = json.loads(raw)
            token = msg.get("token") if isinstance(msg, dict) else None
        except (TimeoutError, ValueError, WebSocketDisconnect):
            token = None
        if not isinstance(token, str) or not secrets.compare_digest(
            token.encode("utf-8"), expected.encode("utf-8")
        ):
            with contextlib.suppress(Exception):
                await ws.close(code=WS_CLOSE_UNAUTHORIZED)
            return
        tail = EventTail()
        try:
            conn = _connect_ro()
        except HTTPException as e:
            await ws.send_json({"type": "error", "data": {"detail": e.detail}})
            await ws.close()
            return
        # входящие сообщения после auth не нужны; приём нужен, чтобы заметить отключение клиента
        receiver = asyncio.ensure_future(ws.receive())
        try:
            tail.prime(conn)
            await ws.send_json({"type": "hello", "data": {"audit_id": tail.audit_id}})
            while True:
                for ev in tail.poll(conn):
                    await ws.send_json(ev.model_dump())
                done, _ = await asyncio.wait({receiver}, timeout=ws_poll_s)
                if done:
                    if receiver.result().get("type") == "websocket.disconnect":
                        break
                    receiver = asyncio.ensure_future(ws.receive())
        except WebSocketDisconnect:
            pass
        finally:
            receiver.cancel()
            conn.close()

    # --- GUI ------------------------------------------------------------------------

    def _page(template: str, active: str) -> Callable[[Request], HTMLResponse]:
        def handler(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(
                request, template, {"active": active, "app_version": __version__}
            )

        return handler

    for url, template in PAGES.items():
        app.add_api_route(
            url,
            _page(template, url),
            methods=["GET"],
            response_class=HTMLResponse,
            include_in_schema=False,
        )

    @app.get("/chain/{ref_id}", response_class=HTMLResponse, include_in_schema=False)
    def chain_page(request: Request, ref_id: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "chain.html",
            {"active": "/signals", "app_version": __version__, "ref_id": ref_id},
        )

    return app
