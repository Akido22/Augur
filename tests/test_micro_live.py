"""MICRO_LIVE e2e (ARCH §7.1, §7.3, §7.4): подтверждение первого ордера, micro-cap, TTL,
сверка spot, kill switch и системные выходы на «реальной» площадке."""

import sqlite3
from decimal import Decimal

import pytest

from augur.adapters import OrderState
from augur.config.settings import RiskLimits, TradingMode
from augur.execution import (
    ApprovalError,
    ExecutionRefusedError,
    InvalidApprovalCodeError,
    build_live_stack,
)
from augur.execution.approval import awaiting_approval, first_order_confirmed
from tests._exec import APP, Clock, audit_events, classic_signal, stack
from tests._live import enter_micro_live, live_settings, live_stack, process_live


async def _first_order(db: sqlite3.Connection) -> tuple:  # type: ignore[type-arg]
    st, clock, venue = live_stack(db)
    await enter_micro_live(db, clock)
    res = await process_live(st, clock, await classic_signal(db, now_ms=clock.now))
    assert res.verdict.approved, res.verdict
    return st, clock, venue, res.order


async def test_first_order_waits_for_approval(db: sqlite3.Connection) -> None:
    st, clock, venue, order = await _first_order(db)
    assert order is not None and order.state is OrderState.PENDING
    assert order.mode == "MICRO_LIVE" and order.venue == "ccxt:fake"
    assert venue.place_calls == 0  # без подтверждения на площадку ничего не ушло
    assert awaiting_approval(db, order.id)
    assert audit_events(db, "ORDER_APPROVAL_REQUIRED") == ["ORDER_APPROVAL_REQUIRED"]
    assert [o.id for o in st.approval.pending()] == [order.id]
    # Reconciler не отклоняет неотправленный ордер, ждущий подтверждения
    report = await st.reconciler.run()
    assert report.ok
    assert st.orders.orders.get(order.id).state is OrderState.PENDING  # type: ignore[union-attr]
    # повторная отправка в обход подтверждения невозможна
    assert (await st.orders.submit(order)).state is OrderState.PENDING
    assert venue.place_calls == 0


async def test_approve_sends_then_next_orders_are_automatic(db: sqlite3.Connection) -> None:
    st, clock, venue, order = await _first_order(db)
    ticket = st.approval.issue_code(order.id)
    rec = await st.approval.approve(order.id, ticket.code, actor="cli:test")
    assert rec.state is OrderState.FILLED and venue.place_calls == 1
    assert first_order_confirmed(db)
    assert audit_events(db, "ORDER_APPROVED") == ["ORDER_APPROVED"]
    exec_result = db.execute(
        "SELECT execution_result FROM decision_audit WHERE order_id = ?", (order.id,)
    ).fetchone()[0]
    assert exec_result is not None and '"state":"FILLED"' in exec_result

    clock.advance(3_600_000)
    sig = await classic_signal(db, now_ms=clock.now, bar_ts=clock.now // 3_600_000 * 3_600_000)
    res = await process_live(st, clock, sig)
    if res.order is not None:  # второй сигнал может быть отклонён чеками — важно, что без ожидания
        assert res.order.state is not OrderState.PENDING
        assert not awaiting_approval(db, res.order.id)
    assert audit_events(db, "ORDER_APPROVAL_REQUIRED") == ["ORDER_APPROVAL_REQUIRED"]


async def test_micro_cap_limits_notional(db: sqlite3.Connection) -> None:
    _, _, _, order = await _first_order(db)
    limits = RiskLimits()
    price = Decimal(str((await live_stack(db)[0].venue_adapter.get_latest("BTC/USDT")).ask))
    assert order.qty * price <= limits.micro_live_max_notional * Decimal("1.01")


async def test_wrong_approval_code(db: sqlite3.Connection) -> None:
    st, _, venue, order = await _first_order(db)
    ticket = st.approval.issue_code(order.id)
    wrong = "000000" if ticket.code != "000000" else "111111"
    with pytest.raises(InvalidApprovalCodeError):
        await st.approval.approve(order.id, wrong, actor="t")
    with pytest.raises(InvalidApprovalCodeError):  # тикет одноразовый
        await st.approval.approve(order.id, ticket.code, actor="t")
    assert venue.place_calls == 0 and not first_order_confirmed(db)


async def test_approval_not_longer_than_signal_ttl(db: sqlite3.Connection) -> None:
    st, clock, venue, order = await _first_order(db)
    ticket = st.approval.issue_code(order.id)
    clock.advance(30 * 60_000 + 1)  # TTL классического 1h-сигнала — полбара
    with pytest.raises((ApprovalError, InvalidApprovalCodeError)):
        await st.approval.approve(order.id, ticket.code, actor="t")
    st2, _, _ = live_stack(db, clock=clock)
    report = await st2.reconciler.run()
    assert order.id in report.updated_orders
    rec = st2.orders.orders.get(order.id)
    assert rec is not None and rec.state is OrderState.REJECTED
    assert "approval timeout" in (rec.reject_reason or "")
    assert venue.place_calls == 0


async def test_approval_refused_outside_micro_live_or_with_cb(db: sqlite3.Connection) -> None:
    st, clock, _, order = await _first_order(db)
    db.execute("UPDATE cb_state SET state = 'TRIPPED' WHERE id = 1")
    db.execute(
        "INSERT OR IGNORE INTO cb_state(id, state, reason, tripped_ts) VALUES (1,'TRIPPED','t',0)"
    )
    db.commit()
    with pytest.raises(ApprovalError, match="circuit breaker"):
        st.approval.issue_code(order.id)
    db.execute("UPDATE cb_state SET state = 'ARMED' WHERE id = 1")
    db.commit()
    st.modes.set_mode(TradingMode.PAPER, actor="t", reason="x", ts=clock.now)
    db.commit()
    with pytest.raises(ApprovalError, match="requires MICRO_LIVE"):
        st.approval.issue_code(order.id)


async def test_reentry_requires_new_approval(db: sqlite3.Connection) -> None:
    st, clock, _, order = await _first_order(db)
    ticket = st.approval.issue_code(order.id)
    await st.approval.approve(order.id, ticket.code, actor="t")
    assert first_order_confirmed(db)
    st.modes.set_mode(TradingMode.PAPER, actor="t", reason="pause", ts=clock.now)
    db.commit()
    await enter_micro_live(db, clock)
    assert not first_order_confirmed(db)  # новый вход в режим — снова ручное подтверждение


async def test_pipeline_skips_when_mode_below_stack(db: sqlite3.Connection) -> None:
    paper, _ = stack(db)
    await paper.orders.snapshot()  # история PAPER: вердикт в PAPER может быть одобрен
    st, clock, venue = live_stack(db)  # режим не повышен: PAPER
    res = await process_live(st, clock, await classic_signal(db, now_ms=clock.now))
    assert res.verdict.mode == TradingMode.PAPER and res.verdict.approved
    assert res.order is None and venue.place_calls == 0
    assert audit_events(db, "EXECUTION_SKIPPED") == ["EXECUTION_SKIPPED"]


async def test_kill_switch_flattens_on_live_venue(db: sqlite3.Connection) -> None:
    st, clock, venue, order = await _first_order(db)
    await st.approval.approve(order.id, st.approval.issue_code(order.id).code, actor="t")
    [pos] = await venue.get_positions()
    assert pos.qty > 0
    report = await st.kill_switch.engage(actor="cli:test", flatten=True)
    assert len(report.flatten_orders) == 1
    assert await venue.get_positions() == []
    assert st.modes.current() == TradingMode.ANALYSIS_ONLY


async def test_system_exit_does_not_wait_for_approval(db: sqlite3.Connection) -> None:
    st, clock, venue, order = await _first_order(db)
    await st.approval.approve(order.id, st.approval.issue_code(order.id).code, actor="t")
    st.modes.set_mode(TradingMode.PAPER, actor="t", reason="pause", ts=clock.now)
    db.commit()
    await enter_micro_live(db, clock)  # флаг подтверждения сброшен
    pos = st.orders.positions.get(st.orders.venue, "BTC/USDT", "MICRO_LIVE")
    assert pos is not None
    db.execute(
        "UPDATE positions SET stop_loss = ? WHERE venue = ? AND mode = 'MICRO_LIVE'",
        ("1000000", st.orders.venue),
    )
    db.commit()
    out = await st.orders.check_protective_exits()
    assert len(out) == 1 and out[0].state is OrderState.FILLED
    assert not awaiting_approval(db, out[0].id)


async def test_spot_reconcile_scope_and_tolerance(db: sqlite3.Connection) -> None:
    st, _, venue = live_stack(db)
    venue.extra_positions["DOGE/USDT"] = Decimal("5")  # пыль вне settings.symbols
    assert await st.reconciler.check_positions() == []
    venue.extra_positions["BTC/USDT"] = Decimal("0.5")  # чужой BTC на субсчёте
    [m] = await st.reconciler.check_positions()
    assert m.symbol == "BTC/USDT" and m.venue_qty == Decimal("0.5")


def test_build_live_stack_guards(db: sqlite3.Connection) -> None:
    clock = Clock()
    with pytest.raises(ExecutionRefusedError, match="does not allow MICRO_LIVE"):
        build_live_stack(
            db, live_settings(trading_mode=TradingMode.PAPER), app_version=APP, clock=clock
        )
    with pytest.raises(ExecutionRefusedError, match="CCXT_API_KEY"):
        build_live_stack(db, live_settings(), app_version=APP, clock=clock)
