"""CLI 0.11.0: `mode promotion`, `mode set FULL_AUTO --approved-by`, `alerts`, `run`."""

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from augur.adapters.paper import PaperAdapter
from augur.notify import Notification
from tests._exec import Clock
from tests._live import FakeLiveVenue
from tests.test_full_auto import _micro_fills


class FakeNotifier:
    sent: list[Notification] = []

    async def send(self, msg: Notification) -> None:
        FakeNotifier.sent.append(msg)


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    from augur.interfaces.cli.main import app

    monkeypatch.chdir(tmp_path)
    for k, v in {
        "API_AUTH_TOKEN": "test-token",
        "TRADING_MODE": "FULL_AUTO",
        "MAX_ALLOWED_MODE": "FULL_AUTO",
        "TELEGRAM_BOT_TOKEN": "1:test",
        "TELEGRAM_CHAT_ID": "42",
        "MODE__MIN_MICRO_TRADES": "3",
        "MODE__MIN_COMPARED_ORDERS": "3",
    }.items():
        monkeypatch.setenv(k, v)
    db = str(tmp_path / "augur.db")
    assert CliRunner().invoke(app, ["db", "migrate", "--db", db]).exit_code == 0
    venue = FakeLiveVenue(Clock(int(time.time() * 1000)))
    venue.set_clock(None)
    monkeypatch.setattr("augur.interfaces.cli.main._live_adapter", lambda _s: venue)
    monkeypatch.setattr("augur.interfaces.cli.main._notifier", lambda _s: FakeNotifier())
    monkeypatch.setattr("augur.interfaces.cli.main._data_adapter", lambda _s: PaperAdapter())
    monkeypatch.setattr("augur.execution.modes.secrets.randbelow", lambda _n: 777)
    FakeNotifier.sent = []
    return db


def _fills(db: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        asyncio.run(_micro_fills(conn, 3, ts=int(time.time() * 1000) + 1_000))
    finally:
        conn.close()


def test_full_auto_promotion_flow(cli: str) -> None:
    from augur.interfaces.cli.main import app

    db, r = cli, CliRunner()
    res = r.invoke(app, ["mode", "set", "MICRO_LIVE", "--confirm", "--db", db], input="000777\n")
    assert res.exit_code == 0, res.output
    res = r.invoke(app, ["mode", "promotion", "--db", db])
    assert res.exit_code == 1 and "NOT READY" in res.output and "trades 0/3" in res.output
    _fills(db)
    res = r.invoke(app, ["mode", "promotion", "--db", db])
    assert res.exit_code == 0 and "READY" in res.output, res.output
    res = r.invoke(app, ["mode", "set", "FULL_AUTO", "--confirm", "--db", db])
    assert res.exit_code == 1 and "approved_by" in res.output
    res = r.invoke(
        app,
        ["mode", "set", "FULL_AUTO", "--confirm", "--approved-by", "Artem", "--db", db],
        input="000777\n",
    )
    assert res.exit_code == 0, res.output
    assert "MICRO_LIVE -> FULL_AUTO" in res.output and "automatically" in res.output
    res = r.invoke(app, ["mode", "show", "--db", db])
    assert "mode:             FULL_AUTO" in res.output

    res = r.invoke(app, ["alerts", "dispatch", "--db", db])
    assert res.exit_code == 0 and "sent=" in res.output, res.output
    kinds = {(n.kind, n.level) for n in FakeNotifier.sent}
    assert ("MODE_CHANGED", "CRITICAL") in kinds
    res = r.invoke(app, ["alerts", "list", "--db", db])
    assert "MODE_CHANGED" in res.output and "sent" in res.output


def test_alerts_test_and_unconfigured(cli: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from augur.interfaces.cli.main import app

    r = CliRunner()
    res = r.invoke(app, ["alerts", "test"])
    assert res.exit_code == 0 and "test alert sent" in res.output
    assert FakeNotifier.sent[-1].kind == "TEST"
    monkeypatch.setattr("augur.interfaces.cli.main._notifier", lambda _s: None)
    res = r.invoke(app, ["alerts", "test"])
    assert res.exit_code == 1 and "not configured" in res.output


def test_run_one_tick(cli: str) -> None:
    from augur.interfaces.cli.main import app

    res = CliRunner().invoke(app, ["run", "--max-ticks", "1", "--db", cli])
    assert res.exit_code == 0, res.output
    assert "augur run: BTC/USDT 1h (TESTNET)" in res.output
    conn = sqlite3.connect(cli)
    try:
        events = {e for (e,) in conn.execute("SELECT event FROM audit_log")}
    finally:
        conn.close()
    assert {"RUN_STARTED", "RUN_STOPPED"} <= events


def test_llm_explain_disabled_by_default(cli: str) -> None:
    from augur.interfaces.cli.main import app

    res = CliRunner().invoke(app, ["llm", "explain", "--db", cli])
    assert res.exit_code == 1 and "disabled" in res.output
