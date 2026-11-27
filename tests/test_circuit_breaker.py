"""Circuit breaker, триггеры, kill switch, Reconciler, CLI (ARCH §7.4, §9.2)."""

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from augur.adapters import FatalAdapterError, RetryableError, VenuePosition
from augur.config.settings import CircuitBreakerSettings, FusionSettings, RiskLimits, TradingMode
from augur.execution import InvalidResetCodeError, ModeStore
from augur.risk.post_trade import CBTrigger, RiskMetrics, RiskMetricsBuilder, evaluate_triggers
from augur.strategies.base import SignalAction
from tests._exec import audit_events, classic_signal, process, stack

H = 3_600_000


def _hits(**m: object) -> list[CBTrigger]:
    return [
        h.trigger
        for h in evaluate_triggers(
            RiskMetrics(**m),  # type: ignore[arg-type]
            limits=RiskLimits(),
            cb=CircuitBreakerSettings(),
            fusion=FusionSettings(),
        )
    ]


def test_triggers_thresholds() -> None:
    assert _hits() == []
    assert _hits(consecutive_losses=3) == []
    assert _hits(consecutive_losses=4) == [CBTrigger.CONSECUTIVE_LOSSES]
    assert _hits(drawdown_pct=Decimal("0.10")) == []
    assert _hits(drawdown_pct=Decimal("0.1001")) == [CBTrigger.DRAWDOWN]
    assert _hits(day_pnl=Decimal(-300), equity_start_of_day=Decimal(10000)) == []
    assert _hits(day_pnl=Decimal("-300.01"), equity_start_of_day=Decimal(10000)) == [
        CBTrigger.DAILY_LOSS
    ]
    assert _hits(adapter_errors_in_window=5) == [CBTrigger.ADAPTER_ERRORS]
    assert _hits(fatal_adapter_error=True) == [CBTrigger.ADAPTER_ERRORS]
    assert _hits(fusion_invalid_streak=5) == [CBTrigger.FUSION_INVALID_STREAK]
    assert _hits(fusion_invalid_ratio=Decimal("0.5")) == []
    assert _hits(fusion_invalid_ratio=Decimal("0.55")) == [CBTrigger.FUSION_INVALID_STREAK]
    assert _hits(reconcile_mismatch=True) == [CBTrigger.RECONCILE_MISMATCH]
    assert _hits(consecutive_losses=9, reconcile_mismatch=True) == [
        CBTrigger.CONSECUTIVE_LOSSES,
        CBTrigger.RECONCILE_MISMATCH,
    ]


def _fused(db: sqlite3.Connection, n: int, valid: int, t0: int) -> None:
    for i in range(n):
        db.execute(
            "INSERT INTO fused_signals(id, signal_id, idempotency_key, api_version, asset, "
            "timestamp, expires_at, action, strength, reason, explanation_ref, "
            "source_breakdown_json, fusion_config_version, valid, created_at) "
            "VALUES (?, ?, ?, '1.0', 'X', 0, 1, 'HOLD', 0, 'r', 'e', '{}', 'f', ?, ?)",
            (f"{valid}{t0}{i}", f"s{valid}{t0}{i}", f"k{valid}{t0}{i}", valid, t0 + i),
        )


def test_metrics_fusion_invalid_streak_and_ratio(db: sqlite3.Connection) -> None:
    b = RiskMetricsBuilder(db, mode="PAPER", fusion=FusionSettings())
    _fused(db, 10, 1, 1000)
    _fused(db, 6, 0, 2000)
    assert b.fusion_invalid(0) == (6, None)  # окно 20 не набрано
    _fused(db, 4, 0, 3000)
    streak, ratio = b.fusion_invalid(0)
    assert streak == 10 and ratio == Decimal("0.5")
    assert b.fusion_invalid(2500) == (4, None)  # после reset счёт заново


async def test_consecutive_losses_trip_cb_after_fills(db: sqlite3.Connection) -> None:
    cfg_limits = {"max_consecutive_losses": 2}
    from tests._exec import settings

    st, clock = stack(db, cfg=settings(risk=cfg_limits))
    for i in range(2):  # BUY → CLOSE: с комиссией и проскальзыванием сделка в минус
        clock.advance(60_000)
        await process(st, clock, await classic_signal(db, now_ms=clock.now, bar_ts=10 * i))
        clock.advance(1)
        res = await process(
            st,
            clock,
            await classic_signal(db, SignalAction.CLOSE, now_ms=clock.now, bar_ts=10 * i + 1),
        )
        assert res.order is not None
    assert st.breaker.state == "TRIPPED"
    assert "consecutive_losses" in (st.breaker.status().reason or "")
    assert audit_events(db, "CB_TRIPPED") == ["CB_TRIPPED"]
    # R2: новый вход отклонён Risk Gate
    clock.advance(60_000)
    res = await process(st, clock, await classic_signal(db, now_ms=clock.now, bar_ts=99))
    assert not res.verdict.approved and "circuit_breaker_armed" in (res.verdict.risk_reason or "")


async def test_trip_cancels_entry_orders_and_is_idempotent(db: sqlite3.Connection) -> None:
    from tests.test_order_manager import _fake_stack

    st, clock, venue = _fake_stack(db)
    venue.place_mode = "submitted"
    res = await process(st, clock, await classic_signal(db))
    assert res.order is not None and res.order.is_active
    assert await st.breaker.trip(CBTrigger.MANUAL, "operator")
    assert not await st.breaker.trip(CBTrigger.DRAWDOWN, "again")
    assert st.orders.orders.get(res.order.id).state.value == "CANCELLED"  # type: ignore[union-attr]
    assert st.modes.current() is TradingMode.PAPER  # PAPER-hold


async def test_reset_requires_valid_one_time_code(db: sqlite3.Connection) -> None:
    st, clock = stack(db)
    await st.breaker.trip(CBTrigger.MANUAL, "x")
    with pytest.raises(InvalidResetCodeError):
        st.breaker.reset(actor="t", confirm_code="000000")  # код не выдавался
    t = st.breaker.issue_reset_code()
    wrong = "111111" if t.code != "111111" else "222222"
    with pytest.raises(InvalidResetCodeError):
        st.breaker.reset(actor="t", confirm_code=wrong)
    with pytest.raises(InvalidResetCodeError):
        st.breaker.reset(actor="t", confirm_code=t.code)  # код сгорел после попытки
    t = st.breaker.issue_reset_code()
    clock.advance(121_000)
    with pytest.raises(InvalidResetCodeError):
        st.breaker.reset(actor="t", confirm_code=t.code)  # истёк
    assert st.breaker.state == "TRIPPED"
    t = st.breaker.issue_reset_code()
    status = st.breaker.reset(actor="cli:op", confirm_code=t.code)
    assert status.state == "ARMED" and status.reset_by == "cli:op"
    assert audit_events(db, "CB_RESET") == ["CB_RESET"]


async def test_adapter_errors_trip_cb(db: sqlite3.Connection) -> None:
    st, _clock = stack(db)
    for _ in range(4):
        st.breaker.record_adapter_error(RetryableError("x"))
    assert await st.breaker.check_adapter_errors() is None
    st.breaker.record_adapter_error(RetryableError("x"))
    hit = await st.breaker.check_adapter_errors()
    assert hit is not None and hit.trigger is CBTrigger.ADAPTER_ERRORS
    assert st.breaker.state == "TRIPPED"


async def test_fatal_adapter_error_trips_cb(db: sqlite3.Connection) -> None:
    st, _clock = stack(db)
    st.breaker.record_adapter_error(FatalAdapterError("auth"))
    assert (await st.breaker.check_adapter_errors()) is not None
    assert "fatal" in (st.breaker.status().reason or "")


def test_mode_store_never_escalates_above_paper(db: sqlite3.Connection) -> None:
    ms = ModeStore(db, configured_mode=TradingMode.FULL_AUTO)
    assert ms.set_mode(TradingMode.PAPER, actor="t", reason="r", ts=1)
    with pytest.raises(PermissionError):
        ms.set_mode(TradingMode.MICRO_LIVE, actor="t", reason="r", ts=2)
    assert not ms.set_mode(TradingMode.PAPER, actor="t", reason="r", ts=3)
    assert (
        ModeStore(db, configured_mode=TradingMode.SIGNAL_ONLY).current() is TradingMode.SIGNAL_ONLY
    )


# --- kill switch ----------------------------------------------------------------------


async def test_kill_switch_flattens_persists_across_restart_and_resets(
    db: sqlite3.Connection,
) -> None:
    st, clock = stack(db)
    await process(st, clock, await classic_signal(db))
    assert st.orders.positions.open("PAPER")
    report = await st.kill_switch.engage(actor="cli:op", flatten=True)
    assert report.was_state == "ARMED" and len(report.flatten_orders) == 1
    assert st.orders.positions.open("PAPER") == [] and await st.broker.get_positions() == []
    assert st.modes.current() is TradingMode.ANALYSIS_ONLY
    assert audit_events(db, "KILL_SWITCH") == ["KILL_SWITCH"]

    # «рестарт»: новый стек на той же БД — состояние KILLED сохранилось
    st2, clock2 = stack(db, clock=clock)
    assert st2.breaker.state == "KILLED"
    clock2.advance(60_000)
    res = await process(st2, clock2, await classic_signal(db, now_ms=clock2.now, bar_ts=5))
    assert not res.verdict.approved
    reason = res.verdict.risk_reason or ""
    assert "mode_allows_trading" in reason and "circuit_breaker_armed" in reason

    t = st2.breaker.issue_reset_code()
    st2.breaker.reset(actor="cli:op", confirm_code=t.code)
    assert st2.breaker.state == "ARMED" and st2.modes.current() is TradingMode.PAPER
    clock2.advance(60_000)
    res = await process(st2, clock2, await classic_signal(db, now_ms=clock2.now, bar_ts=6))
    assert res.verdict.approved and res.order is not None


async def test_kill_switch_without_flatten_keeps_positions(db: sqlite3.Connection) -> None:
    st, clock = stack(db)
    await process(st, clock, await classic_signal(db))
    report = await st.kill_switch.engage(actor="t", flatten=False)
    assert report.flatten_orders == [] and st.orders.positions.open("PAPER")


# --- Reconciler -----------------------------------------------------------------------


async def test_reconcile_mismatch_trips_cb(db: sqlite3.Connection) -> None:
    st, clock = stack(db)
    await process(st, clock, await classic_signal(db))
    assert (await st.reconciler.run()).ok
    st.broker.restore(
        cash=Decimal(10000), positions=[VenuePosition(symbol="BTC/USDT", qty=Decimal(5))]
    )
    report = await st.reconciler.run()
    assert [m.symbol for m in report.mismatches] == ["BTC/USDT"]
    assert audit_events(db, "RECONCILE_MISMATCH") == ["RECONCILE_MISMATCH"]
    assert st.breaker.state == "TRIPPED" and "reconcile_mismatch" in (
        st.breaker.status().reason or ""
    )


async def test_restart_restores_paper_broker_from_db(db: sqlite3.Connection) -> None:
    st, clock = stack(db)
    await process(st, clock, await classic_signal(db))
    st2, _ = stack(db, clock=clock)  # новый PaperAdapter, состояние из SQLite
    assert (await st2.reconciler.run()).ok
    [p] = await st2.broker.get_positions()
    [row] = st2.orders.positions.open("PAPER")
    assert p.qty == row.qty
    snap = st2.orders.tracker.latest("PAPER")
    assert snap is not None and (await st2.broker.get_account_state()).cash == snap.cash


# --- CLI ------------------------------------------------------------------------------


def _cli_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    from augur.interfaces.cli.main import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_AUTH_TOKEN", "test-token")
    db = str(tmp_path / "augur.db")
    assert CliRunner().invoke(app, ["db", "migrate", "--db", db]).exit_code == 0
    return db


def test_cli_kill_switch_status_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from augur.interfaces.cli.main import app

    db = _cli_db(tmp_path, monkeypatch)
    r = CliRunner()
    res = r.invoke(app, ["cb", "status", "--db", db])
    assert res.exit_code == 0 and "state:      ARMED" in res.output
    res = r.invoke(app, ["kill-switch", "--no-flatten", "--db", db])
    assert res.exit_code == 0, res.output
    assert "KILLED" in res.output and "cb reset --confirm" in res.output
    res = r.invoke(app, ["cb", "status", "--db", db])
    assert "state:      KILLED" in res.output and "ANALYSIS_ONLY" in res.output

    assert r.invoke(app, ["cb", "reset", "--db", db]).exit_code == 2  # без --confirm
    monkeypatch.setattr("augur.execution.circuit_breaker.secrets.randbelow", lambda _n: 4242)
    res = r.invoke(app, ["cb", "reset", "--confirm", "--db", db], input="000001\n")
    assert res.exit_code == 1 and "invalid" in res.output
    res = r.invoke(app, ["cb", "reset", "--confirm", "--db", db], input="004242\n")
    assert res.exit_code == 0, res.output
    assert "confirmation code: 004242" in res.output and "ARMED" in res.output
    res = r.invoke(app, ["cb", "status", "--db", db])
    assert "state:      ARMED" in res.output and "mode:       PAPER" in res.output

    conn = sqlite3.connect(db)
    events = [e for (e,) in conn.execute("SELECT event FROM audit_log ORDER BY id")]
    conn.close()
    assert events[-4:] == ["MODE_CHANGED", "KILL_SWITCH", "MODE_CHANGED", "CB_RESET"]


def test_cli_requires_migrated_db_and_lists_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from augur.interfaces.cli.main import app

    r = CliRunner()
    res = r.invoke(app, ["kill-switch", "--db", str(tmp_path / "missing.db")])
    assert res.exit_code == 1 and "augur db migrate" in res.output
    db = _cli_db(tmp_path, monkeypatch)
    res = r.invoke(app, ["orders", "list", "--db", db])
    assert res.exit_code == 0 and "state" in res.output
