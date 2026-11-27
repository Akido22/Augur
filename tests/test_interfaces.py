"""0.9.0 GUI/CLI: ReadService, decision chain, verify-chain, CLI signals/audit, REST, WS, GUI."""

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from typer.testing import CliRunner

from augur.db.audit import append_audit
from augur.interfaces.api import create_app
from augur.interfaces.api.app import token_id
from augur.interfaces.api.events import EventTail, event_type_for
from augur.interfaces.queries import ChainNotFoundError, ReadService, verify_audit_chain
from augur.strategies.base import SignalAction
from tests._exec import classic_signal, process, stack
from tests.conftest import make_settings

TOKEN = "test-token"  # noqa: S105 — тестовый токен из make_settings
AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def _trade(conn: sqlite3.Connection) -> dict[str, str]:
    """PAPER: BUY исполнен → сигналы, decision_audit, ордер, fill, позиция."""
    st, clock = stack(conn)
    sig = await classic_signal(conn)
    res = await process(st, clock, sig)
    assert res.order is not None
    strategy_id = json.loads(
        conn.execute("SELECT parent_ids_json FROM signals WHERE id=?", (sig.signal_id,)).fetchone()[
            0
        ]
    )[0]
    return {"final": sig.signal_id, "strategy": strategy_id, "order": res.order.id}


# --- ReadService / decision chain ------------------------------------------------------


async def test_decision_chain_from_final_strategy_and_order(db: sqlite3.Connection) -> None:
    ids = await _trade(db)
    svc = ReadService(db, settings=make_settings())
    by_final = svc.decision_chain(ids["final"])
    assert by_final.signal.id == ids["final"] and by_final.signal.kind == "final"
    assert [s.id for s in by_final.strategy_signals] == [ids["strategy"]]
    assert (
        by_final.fused_signal is None or by_final.fused_signal.id == by_final.signal.fused_signal_id
    )
    [dec] = by_final.decisions
    assert dec.risk_verdict == "APPROVED" and dec.order_id == ids["order"]
    assert dec.ml_model_version and dec.llm_prompt_version and dec.app_version
    [order] = by_final.orders
    assert order.state == "FILLED" and len(order.fills) == 1
    assert [p.symbol for p in by_final.positions] == ["BTC/USDT"]
    events = [e.event for e in by_final.events]
    assert "RISK_APPROVED" in events and "ORDER_SUBMITTED" in events
    # из стратегического сигнала и из ордера — та же цепочка
    for ref in (ids["strategy"], ids["order"]):
        ch = svc.decision_chain(ref)
        assert ch.requested_id == ref
        assert ch.signal.id == ids["final"] and [o.id for o in ch.orders] == [ids["order"]]
    with pytest.raises(ChainNotFoundError):
        svc.decision_chain("nope")


async def test_status_and_lists(db: sqlite3.Connection) -> None:
    ids = await _trade(db)
    svc = ReadService(db, settings=make_settings())
    st = svc.status()
    assert st.cb_state == "ARMED" and st.configured_mode == "PAPER"
    assert st.open_positions == 1 and st.open_orders == 0 and st.last_audit_id
    assert [s.id for s in svc.signals(kind="final")] == [ids["final"]]
    assert svc.signals(symbol="ETH/USDT") == []
    assert [o.id for o in svc.orders(state="filled")] == [ids["order"]]
    assert svc.positions()[0].qty == svc.orders()[0].filled_qty
    tail = svc.audit_events(after_id=0, limit=2)
    assert [e.id for e in tail] == sorted(e.id for e in tail)


def test_verify_audit_chain_detects_tampering(db: sqlite3.Connection) -> None:
    assert verify_audit_chain(db).ok and verify_audit_chain(db).rows_checked == 0
    for i in range(3):
        append_audit(db, actor="t", event="E", severity="INFO", payload={"i": i}, ts=1000 + i)
    db.commit()
    rep = verify_audit_chain(db)
    assert rep.ok and rep.rows_checked == 3
    # подмена в обход append-only триггера → обнаруживается
    db.execute("DROP TRIGGER audit_no_update")
    db.execute("UPDATE audit_log SET payload_json='{\"i\":99}' WHERE id=2")
    rep = verify_audit_chain(db)
    assert not rep.ok and rep.first_bad_id == 2 and rep.problem == "row_hash mismatch"


def test_event_type_mapping() -> None:
    assert event_type_for("ORDER_SUBMITTED") == "order_update"
    assert event_type_for("RISK_REJECTED") == "risk_verdict"
    assert event_type_for("KILL_SWITCH") == "cb_state"
    assert event_type_for("CB_RESET") == "cb_state"
    assert event_type_for("MODE_CHANGED") == "mode_changed"
    assert event_type_for("NEWS_REACTION") == "news_event"
    assert event_type_for("DATA_ANOMALY") == "audit"


async def test_event_tail_skips_history_and_reports_new(db: sqlite3.Connection) -> None:
    st, clock = stack(db)
    await process(st, clock, await classic_signal(db))
    tail = EventTail()
    tail.prime(db)
    assert tail.poll(db) == []
    clock.advance(60_000)
    sig = await classic_signal(db, SignalAction.CLOSE, now_ms=clock.now, bar_ts=clock.now)
    await process(st, clock, sig)
    types = [e.type for e in tail.poll(db)]
    assert {"signal_ready", "risk_verdict", "order_update", "position_update"} <= set(types)
    assert tail.poll(db) == []


# --- файл БД для CLI и REST --------------------------------------------------------------


@pytest.fixture
def file_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, dict[str, str]]:
    from augur.interfaces.cli.main import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_AUTH_TOKEN", TOKEN)
    path = str(tmp_path / "augur.db")
    assert CliRunner().invoke(app, ["db", "migrate", "--db", path]).exit_code == 0
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        ids = asyncio.run(_trade(conn))
        conn.commit()
    finally:
        conn.close()
    return path, ids


def test_cli_signals_and_audit(file_db: tuple[str, dict[str, str]]) -> None:
    from augur.interfaces.cli.main import app

    path, ids = file_db
    r = CliRunner()
    res = r.invoke(app, ["signals", "list", "--db", path])
    assert res.exit_code == 0, res.output
    assert ids["final"] in res.output and ids["strategy"] in res.output
    res = r.invoke(
        app, ["signals", "list", "--kind", "final", "--since", "2000-01-01", "--db", path]
    )
    assert res.exit_code == 0 and ids["strategy"] not in res.output
    assert r.invoke(app, ["signals", "list", "--since", "yesterday", "--db", path]).exit_code == 2
    assert r.invoke(app, ["signals", "list", "--kind", "x", "--db", path]).exit_code == 2

    res = r.invoke(app, ["signals", "explain", ids["order"], "--db", path])
    assert res.exit_code == 0, res.output
    out = res.output
    assert f"signal {ids['final']} [final]" in out and "decision" in out and "APPROVED" in out
    assert f"order {ids['order']} FILLED" in out and "fill " in out and "RISK_APPROVED" in out
    res = r.invoke(app, ["audit", "explain", ids["strategy"], "--json", "--db", path])
    assert res.exit_code == 0 and json.loads(res.output)["signal"]["id"] == ids["final"]
    res = r.invoke(app, ["audit", "explain", "missing", "--db", path])
    assert res.exit_code == 1 and "no signal or order" in res.output

    res = r.invoke(app, ["audit", "verify-chain", "--db", path])
    assert res.exit_code == 0 and "hash-chain OK" in res.output
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute("UPDATE audit_log SET actor='evil' WHERE id=1")
    conn.commit()
    conn.close()
    res = r.invoke(app, ["audit", "verify-chain", "--db", path])
    assert res.exit_code == 1 and "BROKEN at id 1" in res.output


def test_cli_kill_switch_still_works(file_db: tuple[str, dict[str, str]]) -> None:
    from augur.interfaces.cli.main import app

    path, _ = file_db
    res = CliRunner().invoke(app, ["kill-switch", "--db", path])
    assert res.exit_code == 0, res.output
    assert "KILLED" in res.output and "flatten orders:   1" in res.output


# --- REST / WS / GUI -----------------------------------------------------------------


def _client(path: str, **kw: Any) -> TestClient:
    return TestClient(create_app(make_settings(), db_path=path, **kw))


def _count(path: str, sql: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


def test_api_requires_bearer_token(file_db: tuple[str, dict[str, str]]) -> None:
    path, _ = file_db
    c = _client(path)
    assert c.get("/api/v1/status").status_code == 401
    assert c.get("/api/v1/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.post("/api/v1/kill-switch", json={}).status_code == 401
    assert _count(path, "SELECT COUNT(*) FROM audit_log WHERE event='KILL_SWITCH'") == 0
    # документация с внешним JS выключена
    assert c.get("/docs").status_code == 404


def test_api_read_endpoints(file_db: tuple[str, dict[str, str]]) -> None:
    path, ids = file_db
    c = _client(path)
    audit_before = _count(path, "SELECT COUNT(*) FROM audit_log")
    st = c.get("/api/v1/status", headers=AUTH).json()
    assert st["cb_state"] == "ARMED" and st["open_positions"] == 1
    sigs = c.get("/api/v1/signals", params={"kind": "final"}, headers=AUTH).json()
    assert [s["id"] for s in sigs] == [ids["final"]]
    assert c.get("/api/v1/signals", params={"kind": "bad"}, headers=AUTH).status_code == 422
    chain = c.get(f"/api/v1/signals/{ids['strategy']}/chain", headers=AUTH).json()
    assert chain["signal"]["id"] == ids["final"]
    assert chain["decisions"][0]["order_id"] == ids["order"]
    chain = c.get(f"/api/v1/orders/{ids['order']}/chain", headers=AUTH).json()
    assert chain["orders"][0]["fills"]
    assert c.get("/api/v1/signals/none/chain", headers=AUTH).status_code == 404
    orders = c.get("/api/v1/orders", params={"state": "FILLED"}, headers=AUTH).json()
    assert [o["id"] for o in orders] == [ids["order"]]
    assert len(c.get("/api/v1/positions", headers=AUTH).json()) == 1
    assert isinstance(c.get("/api/v1/portfolio/snapshots", headers=AUTH).json(), list)
    assert c.get("/api/v1/news", params={"symbol": "BTC/USDT"}, headers=AUTH).json() == []
    assert c.get("/api/v1/news/reactions", headers=AUTH).json() == []
    assert c.get("/api/v1/audit", params={"limit": 5}, headers=AUTH).json()
    assert c.get("/api/v1/audit/verify-chain", headers=AUTH).json()["ok"] is True
    # чтение ничего не пишет
    assert _count(path, "SELECT COUNT(*) FROM audit_log") == audit_before


def test_api_kill_switch_audits_token_fingerprint(file_db: tuple[str, dict[str, str]]) -> None:
    path, _ = file_db
    c = _client(path)
    r = c.post("/api/v1/kill-switch", json={"flatten": True}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["was_state"] == "ARMED" and body["state"] == "KILLED"
    assert body["mode"] == "ANALYSIS_ONLY" and body["flatten_orders"] == 1
    conn = sqlite3.connect(path)
    try:
        actor, payload = conn.execute(
            "SELECT actor, payload_json FROM audit_log WHERE event='KILL_SWITCH'"
        ).fetchone()
        dump = "\n".join(str(r) for r in conn.execute("SELECT * FROM audit_log"))
    finally:
        conn.close()
    assert actor == f"api:{token_id(TOKEN)}"
    assert TOKEN not in dump and TOKEN not in payload
    st = c.get("/api/v1/status", headers=AUTH).json()
    assert st["cb_state"] == "KILLED" and st["mode"] == "ANALYSIS_ONLY"
    # снятия через API нет — только CLI `cb reset --confirm`
    assert c.post("/api/v1/cb/reset", headers=AUTH).status_code in (404, 405)


def test_api_has_no_dangerous_commands(file_db: tuple[str, dict[str, str]]) -> None:
    path, _ = file_db
    app = create_app(make_settings(), db_path=path)
    writes = {
        (route.path, method)
        for route in app.routes
        for method in getattr(route, "methods", None) or ()
        if method not in ("GET", "HEAD")
    }
    assert writes == {("/api/v1/kill-switch", "POST")}


def test_api_missing_or_old_db(tmp_path: Path) -> None:
    c = _client(str(tmp_path / "none.db"))
    assert c.get("/api/v1/status", headers=AUTH).status_code == 503
    old = tmp_path / "old.db"
    sqlite3.connect(old).close()
    assert _client(str(old)).get("/api/v1/status", headers=AUTH).status_code == 503


def test_gui_pages_render_without_data(file_db: tuple[str, dict[str, str]]) -> None:
    path, ids = file_db
    c = _client(path)
    for url in ("/", "/positions", "/signals", "/news", "/control", f"/chain/{ids['final']}"):
        r = c.get(url)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"], url
        assert "/static/app.js" in r.text
        assert ids["order"] not in r.text  # данные — только через REST с токеном
    r = c.get("/chain/%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E")
    assert r.status_code == 200 and "<img src=x" not in r.text and "&lt;img" in r.text
    assert c.get("/static/app.js").status_code == 200
    assert c.get("/static/app.css").status_code == 200


def test_ws_rejects_bad_token(file_db: tuple[str, dict[str, str]]) -> None:
    path, _ = file_db
    c = _client(path, ws_poll_s=0.05)
    with c.websocket_connect("/ws/events") as ws:
        ws.send_json({"type": "auth", "token": "wrong"})
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
    assert ei.value.code == 4401


def test_ws_streams_new_events(file_db: tuple[str, dict[str, str]]) -> None:
    path, _ = file_db
    c = _client(path, ws_poll_s=0.05)
    with c.websocket_connect("/ws/events") as ws:
        ws.send_json({"type": "auth", "token": TOKEN})
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["data"]["audit_id"] > 0
        conn = sqlite3.connect(path)
        append_audit(
            conn,
            actor="cli:t",
            event="MODE_CHANGED",
            severity="INFO",
            ref_type="mode",
            payload={"to": "PAPER"},
            ts=int(time.time() * 1000),
        )
        conn.commit()
        conn.close()
        msg = ws.receive_json()
        assert msg["type"] == "mode_changed" and msg["data"]["event"] == "MODE_CHANGED"
        assert msg["data"]["id"] == hello["data"]["audit_id"] + 1
