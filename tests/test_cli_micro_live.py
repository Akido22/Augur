"""CLI MICRO_LIVE: `mode show|set`, `orders approve`, `reconcile`, kill switch на площадке."""

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from augur.adapters import OrderState
from augur.config.settings import Settings
from tests._exec import Clock, classic_signal
from tests._live import FakeLiveVenue, live_settings, live_stack, process_live


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, FakeLiveVenue]:
    from augur.interfaces.cli.main import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "MICRO_LIVE")
    monkeypatch.setenv("MAX_ALLOWED_MODE", "MICRO_LIVE")
    db = str(tmp_path / "augur.db")
    assert CliRunner().invoke(app, ["db", "migrate", "--db", db]).exit_code == 0
    venue = FakeLiveVenue(Clock(int(time.time() * 1000)))
    venue.set_clock(None)  # реальное время: CLI проверяет TTL по системным часам

    def adapter(_s: Settings) -> FakeLiveVenue:
        return venue

    monkeypatch.setattr("augur.interfaces.cli.main._live_adapter", adapter)
    # один модуль secrets на mode set и orders approve → код 000777 у обоих
    monkeypatch.setattr("augur.execution.modes.secrets.randbelow", lambda _n: 777)
    return db, venue


def _events(db: str) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [e for (e,) in conn.execute("SELECT event FROM audit_log ORDER BY id")]
    finally:
        conn.close()


def test_mode_set_micro_live_with_code(cli: tuple[str, FakeLiveVenue]) -> None:
    from augur.interfaces.cli.main import app

    db, venue = cli
    r = CliRunner()
    res = r.invoke(app, ["mode", "show", "--db", db])
    assert res.exit_code == 0 and "mode:             PAPER" in res.output
    assert r.invoke(app, ["mode", "set", "MICRO_LIVE", "--db", db]).exit_code == 2
    res = r.invoke(app, ["mode", "set", "MICRO_LIVE", "--confirm", "--db", db], input="000001\n")
    assert res.exit_code == 1 and "invalid" in res.output
    res = r.invoke(app, ["mode", "set", "MICRO_LIVE", "--confirm", "--db", db], input="000777\n")
    assert res.exit_code == 0, res.output
    assert "TESTNET" in res.output and "PAPER -> MICRO_LIVE" in res.output
    assert "orders approve" in res.output
    assert not venue.connected  # соединение закрыто
    res = r.invoke(app, ["mode", "set", "FULL_AUTO", "--confirm", "--db", db])
    assert res.exit_code == 1 and "above settings" in res.output
    res = r.invoke(app, ["mode", "set", "PAPER", "--confirm", "--db", db])  # понижение без кода
    assert res.exit_code == 0 and "MICRO_LIVE -> PAPER" in res.output
    assert _events(db)[-3:] == ["MODE_CHANGE_REQUESTED", "MODE_CHANGED", "MODE_CHANGED"]


def test_mode_set_refused_when_subaccount_holds_base_asset(
    cli: tuple[str, FakeLiveVenue],
) -> None:
    from decimal import Decimal

    from augur.interfaces.cli.main import app

    db, venue = cli
    venue.extra_positions["BTC/USDT"] = Decimal("0.3")
    res = CliRunner().invoke(app, ["mode", "set", "MICRO_LIVE", "--confirm", "--db", db])
    assert res.exit_code == 1 and "dedicated subaccount" in res.output
    assert "MODE_CHANGE_REQUESTED" not in _events(db)


async def _create_first_order(db: str, venue: FakeLiveVenue) -> Any:
    """Пайплайн (в будущем — оркестратор) создаёт первый ордер: PENDING, ждёт подтверждения."""
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        now = int(time.time() * 1000)
        clock = Clock(now)
        st, _, _ = live_stack(conn, clock=clock, cfg=live_settings())
        st.orders.adapter._inner = venue  # type: ignore[attr-defined]  # одна площадка с CLI
        res = await process_live(st, clock, await classic_signal(conn, now_ms=now))
        return res.order
    finally:
        conn.close()


def test_orders_approve_sends_first_order(cli: tuple[str, FakeLiveVenue]) -> None:
    from augur.interfaces.cli.main import app

    db, venue = cli
    r = CliRunner()
    res = r.invoke(app, ["mode", "set", "MICRO_LIVE", "--confirm", "--db", db], input="000777\n")
    assert res.exit_code == 0, res.output
    order = asyncio.run(_create_first_order(db, venue))
    assert order is not None and order.state is OrderState.PENDING

    res = r.invoke(app, ["mode", "show", "--db", db])
    assert order.id in res.output and "awaits approval" in res.output
    assert r.invoke(app, ["orders", "approve", order.id, "--db", db]).exit_code == 2
    res = r.invoke(app, ["orders", "approve", "nope", "--confirm", "--db", db])
    assert res.exit_code == 1 and "not found" in res.output
    res = r.invoke(app, ["orders", "approve", order.id, "--confirm", "--db", db], input="000777\n")
    assert res.exit_code == 0, res.output
    assert "FILLED" in res.output and venue.place_calls == 1
    res = r.invoke(app, ["reconcile", "--db", db])
    assert res.exit_code == 0, res.output
    events = _events(db)
    assert "ORDER_APPROVAL_REQUIRED" in events and "ORDER_APPROVED" in events


def test_kill_switch_uses_live_venue(cli: tuple[str, FakeLiveVenue]) -> None:
    from augur.interfaces.cli.main import app

    db, venue = cli
    res = CliRunner().invoke(app, ["kill-switch", "--db", db])
    assert res.exit_code == 0, res.output
    assert "KILLED" in res.output and "WARNING" not in res.output
    assert "KILL_SWITCH" in _events(db)


def test_kill_switch_without_venue_still_kills(
    cli: tuple[str, FakeLiveVenue], monkeypatch: pytest.MonkeyPatch
) -> None:
    from augur.interfaces.cli.main import app

    db, venue = cli

    async def down() -> None:
        raise ConnectionError("no route")

    monkeypatch.setattr(venue, "connect", down)
    res = CliRunner().invoke(app, ["kill-switch", "--db", db])
    assert res.exit_code == 0, res.output
    assert "NOT touched" in res.output and "KILLED" in res.output
    assert "KILL_SWITCH" in _events(db)
