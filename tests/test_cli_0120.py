"""CLI 0.12.0: macro validate/list, ml nightly, fusion breaker list/reset, fusion serve/health."""

import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from augur.fusion.breaker import asset_breaker_open, open_asset_breaker
from augur.interfaces.cli.main import app

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.chdir(tmp_path)
    for k in ("CCXT_API_KEY", "CCXT_API_SECRET", "FUSION__ENDPOINT", "FUSION_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("API_AUTH_TOKEN", "t")
    db = str(tmp_path / "a.db")
    assert runner.invoke(app, ["db", "migrate", "--db", db]).exit_code == 0
    return db


def _cal(path: Path, minutes: int = 30) -> None:
    when = (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"events:\n  - id: evt_cpi\n    name: US CPI\n    time: '{when}'\n    impact: high\n"
        f"    scope: [USD]\n  - id: evt_ecb\n    name: ECB\n    time: '{when}'\n"
        "    impact: medium\n    scope: [EUR]\n",
        encoding="utf-8",
    )


def test_macro_validate_and_list(env: str) -> None:
    res = runner.invoke(app, ["macro", "validate"])
    assert res.exit_code == 1 and "not found" in res.output
    _cal(Path("config/macro_calendar.yaml"))
    res = runner.invoke(app, ["macro", "validate"])
    assert res.exit_code == 0 and "2 events (1 high-impact)" in res.output
    res = runner.invoke(app, ["macro", "list", "--symbol", "BTC/USDT"])
    assert res.exit_code == 0
    assert "* " in res.output and "evt_cpi" in res.output and "evt_ecb" not in res.output
    Path("config/macro_calendar.yaml").write_text("events: [x\n", encoding="utf-8")
    res = runner.invoke(app, ["macro", "validate"])
    assert res.exit_code == 1 and "ERROR" in res.output


def test_ml_nightly_without_model(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION__ENABLED", "true")
    res = runner.invoke(app, ["ml", "nightly", "--symbol", "BTC/USDT", "--db", env])
    assert res.exit_code == 0, res.output
    assert "BTC/USDT 1h -: no_model" in res.output


def test_fusion_breaker_list_and_reset(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    res = runner.invoke(app, ["fusion", "breaker", "list", "--db", env])
    assert "no open Fusion breakers" in res.output
    conn = sqlite3.connect(env)
    open_asset_breaker(
        conn,
        "BTC/USDT",
        trigger="ml_drift",
        detail={"model_version": "lightgbm_v1"},
        actor="t",
        ts=1,
    )
    conn.commit()
    res = runner.invoke(app, ["fusion", "breaker", "list", "--db", env])
    assert "BTC/USDT" in res.output and "ml_drift" in res.output and "lightgbm_v1" in res.output
    base = ["fusion", "breaker", "reset", "--symbol", "BTC/USDT", "--approved-by", "Artem"]
    assert runner.invoke(app, [*base, "--db", env]).exit_code == 2
    monkeypatch.setattr(secrets, "randbelow", lambda _n: 42)
    res = runner.invoke(app, [*base, "--confirm", "--db", env], input="999999\n")
    assert res.exit_code == 1 and asset_breaker_open(conn, "BTC/USDT")
    res = runner.invoke(app, [*base, "--confirm", "--db", env], input="000042\n")
    assert res.exit_code == 0, res.output
    assert "CLOSED" in res.output and not asset_breaker_open(conn, "BTC/USDT")
    approved = conn.execute(
        "SELECT json_extract(payload_json, '$.approved_by') FROM audit_log "
        "WHERE event='FUSION_BREAKER_CLOSED'"
    ).fetchone()[0]
    assert approved == "Artem"
    conn.close()


def test_fusion_serve_refusals_and_start(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    started: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda api, **kw: started.update(api=api, **kw))
    res = runner.invoke(app, ["fusion", "serve", "--db", env])
    assert res.exit_code == 1 and "FUSION_API_TOKEN" in res.output
    monkeypatch.setenv("FUSION_API_TOKEN", "srv-token")
    res = runner.invoke(app, ["fusion", "serve", "--db", env])
    assert res.exit_code == 1 and "FUSION__ENABLED" in res.output
    monkeypatch.setenv("FUSION__ENABLED", "true")
    monkeypatch.setenv("CCXT_API_KEY", "xk-SECRET-123")
    res = runner.invoke(app, ["fusion", "serve", "--db", env])
    assert res.exit_code == 1 and "CCXT_API_KEY" in res.output and "xk-SECRET-123" not in res.output
    assert started == {}
    monkeypatch.delenv("CCXT_API_KEY")
    monkeypatch.setenv("FUSION__ENDPOINT", "http://127.0.0.1:8100")  # игнорируется сервисом
    res = runner.invoke(app, ["fusion", "serve", "--db", env, "--no-jobs", "--port", "8123"])
    assert res.exit_code == 0, res.output
    assert started["port"] == 8123 and started["host"] == "127.0.0.1"


def test_fusion_serve_env_file(env: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    started: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda api, **kw: started.update(kw))
    f = tmp_path / ".env.fusion"
    f.write_text("API_AUTH_TOKEN=x\nFUSION_API_TOKEN=y\nFUSION__ENABLED=true\n", encoding="utf-8")
    res = runner.invoke(app, ["fusion", "serve", "--db", env, "--env-file", str(f), "--no-jobs"])
    assert res.exit_code == 0, res.output and started
    res = runner.invoke(app, ["fusion", "serve", "--db", env, "--env-file", "missing.env"])
    assert res.exit_code == 1


def test_fusion_health_requires_endpoint(env: str) -> None:
    res = runner.invoke(app, ["fusion", "health"])
    assert res.exit_code == 1 and "FUSION__ENDPOINT" in res.output
