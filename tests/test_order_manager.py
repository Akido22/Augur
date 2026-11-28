"""OrderManager (ARCH §7.3, VERSIONING §6.2): аудит до отправки, идемпотентность, восстановление
после сетевых сбоев, частичные исполнения, отказы, блокировки CB, защитные выходы."""

import json
import sqlite3
from decimal import Decimal
from typing import Any

import pytest

from augur.adapters import (
    OrderAck,
    OrderRequest,
    OrderState,
    PaperAdapter,
    RejectedError,
    RetryableError,
    VenueFill,
)
from augur.config.settings import TradingMode
from augur.execution import ExecutionRefusedError, OrderManager, PaperStack
from augur.execution.positions import PositionManager
from augur.risk import DecisionAuditWriter, RiskContextBuilder, RiskGate, market_snapshot
from augur.risk.audit import UnauditableOrderError
from augur.risk.models import RiskVerdict
from augur.risk.post_trade import CBTrigger
from augur.signals import SignalReady
from augur.signals.ready import ms_to_dt
from augur.strategies.base import SignalAction
from tests._exec import APP, Clock, audit_events, bars, classic_signal, process, stack


class FakeVenue(PaperAdapter):
    """PaperAdapter с инъекцией сбоев `place_order` / `get_order`."""

    def __init__(self, clock: Clock, **kw: Any) -> None:
        super().__init__(clock_ms=clock.now, **kw)
        self.place_mode = "ok"  # ok | lost_ack | not_sent | down | reject | submitted
        self.get_down = False
        self.place_calls = 0

    async def place_order(self, req: OrderRequest) -> OrderAck:
        self.place_calls += 1
        mode = self.place_mode
        if mode == "lost_ack":  # исполнен, но ответ потерян
            await super().place_order(req)
            self.place_mode = "ok"
            raise RetryableError("timeout")
        if mode == "not_sent":  # не дошёл до площадки
            self.place_mode = "ok"
            raise RetryableError("timeout")
        if mode == "down":
            raise RetryableError("connection refused")
        if mode == "reject":
            raise RejectedError("insufficient funds")
        if mode == "submitted":  # принят, исполнение потом (частичные fills)
            ack = OrderAck(
                client_order_id=req.client_order_id,
                venue_order_id="v-1",
                state=OrderState.SUBMITTED,
            )
            self._orders[req.client_order_id] = ack
            return ack
        return await super().place_order(req)

    async def get_order(self, client_order_id: str, symbol: str) -> OrderAck | None:
        if self.get_down:
            raise RetryableError("connection refused")
        return await super().get_order(client_order_id, symbol)

    async def cancel_order(self, client_order_id: str, symbol: str) -> OrderAck:
        ack = self._orders[client_order_id]
        if ack.state in (OrderState.SUBMITTED, OrderState.PARTIAL):
            ack = ack.model_copy(update={"state": OrderState.CANCELLED})
            self._orders[client_order_id] = ack
        return ack


def _fake_stack(db: sqlite3.Connection, **kw: Any) -> tuple[PaperStack, Clock, FakeVenue]:
    clock = Clock()
    venue = FakeVenue(clock, **kw)
    from augur.adapters import CallPolicy

    st, _ = stack(db, clock=clock, broker=venue)
    st.adapter._policy = CallPolicy(max_retries=0, timeout_s=5)  # без пауз в тестах
    return st, clock, venue


async def decide(st: PaperStack, clock: Clock, sig: SignalReady) -> tuple[RiskVerdict, str]:
    """То же, что pipeline.process, но без отправки: вердикт + закоммиченный decision_audit."""
    conn = st.orders.orders.conn
    await st.orders.snapshot()
    market = await market_snapshot(st.adapter, sig.symbol, bars=await bars(st, clock))
    ctx = RiskContextBuilder(conn, configured_mode=TradingMode.PAPER).build(
        sig, market, now=ms_to_dt(clock.now)
    )
    v = await RiskGate(st.pipeline._gate.limits).evaluate(sig, ctx)
    rec = DecisionAuditWriter(conn, app_version=APP, config_hash="c" * 64).record(sig, v)
    return v, rec.id


# --- аудит и вердикт ------------------------------------------------------------------


async def test_rejected_verdict_creates_no_order(db: sqlite3.Connection) -> None:
    st, clock = stack(db)
    res = await process(st, clock, await classic_signal(db, SignalAction.HOLD))
    assert not res.verdict.approved and res.order is None
    assert db.execute("SELECT COUNT(*) FROM orders").fetchone() == (0,)
    assert db.execute("SELECT risk_verdict FROM decision_audit").fetchone() == ("REJECTED",)


async def test_no_decision_audit_no_order(db: sqlite3.Connection) -> None:
    st, clock = stack(db)
    sig = await classic_signal(db)
    market = await market_snapshot(st.adapter, sig.symbol, bars=await bars(st, clock))
    await st.orders.snapshot()
    ctx = RiskContextBuilder(db, configured_mode=TradingMode.PAPER).build(
        sig, market, now=ms_to_dt(clock.now)
    )
    v = await RiskGate(st.pipeline._gate.limits).evaluate(sig, ctx)
    assert v.approved
    with pytest.raises(ExecutionRefusedError, match="decision_audit"):
        await st.orders.handle_verdict(v, sig)
    assert db.execute("SELECT COUNT(*) FROM orders").fetchone() == (0,)
    assert st.broker._orders == {}


async def test_unauditable_decision_blocks_order(db: sqlite3.Connection) -> None:
    """VERSIONING §6.2: «плавающая» версия в записи → ордер не создаётся и не отправляется."""
    st, clock = stack(db)
    sig = await classic_signal(db)
    v, _ = await decide(st, clock, sig)
    db.execute(
        "INSERT INTO decision_audit(id, signal_id, app_version, strategy_version, "
        "ml_model_version, llm_prompt_version, risk_verdict, config_hash, mode, created_at) "
        "VALUES ('01ZZZZZZZZZZZZZZZZZZZZZZZZ', ?, '0.6.0', 'latest', 'n/a', 'n/a', 'APPROVED', "
        "'c', 'PAPER', 1)",
        (sig.signal_id,),
    )
    db.commit()
    with pytest.raises(UnauditableOrderError, match="strategy_version"):
        await st.orders.handle_verdict(v, sig, decision_id="01ZZZZZZZZZZZZZZZZZZZZZZZZ")
    assert db.execute("SELECT COUNT(*) FROM orders").fetchone() == (0,)


def test_mode_and_venue_must_match(db: sqlite3.Connection) -> None:
    kw: dict[str, Any] = {"app_version": APP, "config_hash": "c", "clock": lambda: 0}
    with pytest.raises(ExecutionRefusedError, match="real venue"):
        OrderManager(db, PaperAdapter(), mode=TradingMode.MICRO_LIVE, **kw)

    class Live(PaperAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.venue = "binance"

    with pytest.raises(ExecutionRefusedError, match="paper venue"):
        OrderManager(db, Live(), mode=TradingMode.PAPER, **kw)
    with pytest.raises(ExecutionRefusedError, match="real venue"):
        OrderManager(db, PaperAdapter(), mode=TradingMode.FULL_AUTO, **kw)
    with pytest.raises(ExecutionRefusedError, match="SIGNAL_ONLY"):
        OrderManager(db, Live(), mode=TradingMode.SIGNAL_ONLY, **kw)
    assert OrderManager(db, Live(), mode=TradingMode.FULL_AUTO, **kw).mode == TradingMode.FULL_AUTO
    assert OrderManager(db, Live(), mode=TradingMode.MICRO_LIVE, **kw).venue == "binance"


# --- сеть: place_order не повторяется вслепую ----------------------------------------


async def test_lost_ack_is_recovered_by_get_order(db: sqlite3.Connection) -> None:
    st, clock, venue = _fake_stack(db)
    venue.place_mode = "lost_ack"
    res = await process(st, clock, await classic_signal(db))
    assert res.order is not None and res.order.state is OrderState.FILLED
    assert venue.place_calls == 1  # повторной отправки не было
    [pos] = await venue.get_positions()
    assert pos.qty == res.order.filled_qty  # одна сделка
    assert "UNKNOWN" not in json.dumps(
        [r for r in db.execute("SELECT payload_json FROM audit_log")]
    )


async def test_not_sent_is_resent_with_same_client_order_id(db: sqlite3.Connection) -> None:
    st, clock, venue = _fake_stack(db)
    venue.place_mode = "not_sent"
    res = await process(st, clock, await classic_signal(db))
    assert res.order is not None and res.order.state is OrderState.FILLED
    assert venue.place_calls == 2
    assert list(venue._orders) == [res.order.client_order_id]


async def test_unknown_status_blocks_symbol_until_reconcile(db: sqlite3.Connection) -> None:
    st, clock, venue = _fake_stack(db)
    venue.place_mode, venue.get_down = "down", True
    res = await process(st, clock, await classic_signal(db))
    assert res.order is not None and res.order.state is OrderState.UNKNOWN
    assert (
        json.loads(db.execute("SELECT execution_result FROM decision_audit").fetchone()[0])["state"]
        == "UNKNOWN"
    )
    # R5: новый вход по символу запрещён, пока ордер в UNKNOWN
    venue.place_mode, venue.get_down = "ok", False
    clock.advance(60_000)
    res2 = await process(st, clock, await classic_signal(db, now_ms=clock.now, bar_ts=clock.now))
    assert not res2.verdict.approved and "no_unknown_orders" in (res2.verdict.risk_reason or "")
    # Реконсиляция: площадка ордера не знает → REJECTED
    report = await st.reconciler.run()
    assert report.updated_orders == [res.order.id] and report.ok
    assert st.orders.orders.get(res.order.id).state is OrderState.REJECTED  # type: ignore[union-attr]


async def test_unknown_that_was_filled_reconciles_to_filled(db: sqlite3.Connection) -> None:
    st, clock, venue = _fake_stack(db)
    venue.place_mode = "lost_ack"
    venue.get_down = True  # статус не установить
    res = await process(st, clock, await classic_signal(db))
    assert res.order is not None and res.order.state is OrderState.UNKNOWN
    venue.get_down = False
    report = await st.reconciler.run()
    rec = st.orders.orders.get(res.order.id)
    assert rec is not None and rec.state is OrderState.FILLED and report.ok
    [pos] = st.orders.positions.open("PAPER")
    assert pos.qty == rec.filled_qty  # позиция БД = площадка


async def test_venue_rejection_is_terminal_and_audited(db: sqlite3.Connection) -> None:
    st, clock, venue = _fake_stack(db)
    venue.place_mode = "reject"
    res = await process(st, clock, await classic_signal(db))
    assert res.order is not None and res.order.state is OrderState.REJECTED
    assert res.order.reject_reason == "insufficient funds"
    result = json.loads(db.execute("SELECT execution_result FROM decision_audit").fetchone()[0])
    assert result["state"] == "REJECTED" and result["reject_reason"] == "insufficient funds"
    assert st.orders.positions.open("PAPER") == []


# --- частичные исполнения -------------------------------------------------------------


def _partial(coid: str, state: OrderState, fills: list[tuple[str, str]]) -> OrderAck:
    vf = [
        VenueFill(
            venue_fill_id=f"f{i}", qty=Decimal(q), price=Decimal(p), fee=Decimal("0.01"), ts=1
        )
        for i, (q, p) in enumerate(fills)
    ]
    total = sum((f.qty for f in vf), Decimal(0))
    return OrderAck(
        client_order_id=coid, venue_order_id="v-1", state=state, filled_qty=total, fills=vf
    )


async def test_partial_fills_are_deduplicated_and_cancel_keeps_filled(
    db: sqlite3.Connection,
) -> None:
    st, clock, venue = _fake_stack(db)
    venue.place_mode = "submitted"
    res = await process(st, clock, await classic_signal(db))
    order = res.order
    assert order is not None and order.state is OrderState.SUBMITTED
    upd = _partial(order.client_order_id, OrderState.PARTIAL, [("0.5", "100")])
    await st.orders.on_venue_update(upd)
    await st.orders.on_venue_update(upd)  # дубль (реконнект) — второго fill нет
    rec = st.orders.orders.get(order.id)
    assert rec is not None and rec.state is OrderState.PARTIAL and rec.filled_qty == Decimal("0.5")
    assert len(st.orders.orders.fills(order.id)) == 1
    upd2 = _partial(order.client_order_id, OrderState.PARTIAL, [("0.5", "100"), ("0.25", "102")])
    venue._orders[order.client_order_id] = upd2
    rec = await st.orders.on_venue_update(upd2)
    assert (
        rec is not None
        and rec.filled_qty == Decimal("0.75")
        and rec.avg_fill_price == Decimal("100.66666667")
    )
    [pos] = st.orders.positions.open("PAPER")
    assert pos.qty == Decimal("0.75")  # позиция по каждому fill
    # остаток отменяется по таймауту partial_timeout_s
    assert await st.orders.expire_partials() == 0
    clock.advance(61_000)
    assert await st.orders.expire_partials() == 1
    rec = st.orders.orders.get(order.id)
    assert (
        rec is not None and rec.state is OrderState.CANCELLED and rec.filled_qty == Decimal("0.75")
    )


async def test_late_update_on_terminal_order_is_ignored(db: sqlite3.Connection) -> None:
    st, clock = stack(db)
    res = await process(st, clock, await classic_signal(db))
    assert res.order is not None
    late = OrderAck(
        client_order_id=res.order.client_order_id, venue_order_id="x", state=OrderState.CANCELLED
    )
    rec = await st.orders.on_venue_update(late)
    assert rec is not None and rec.state is OrderState.FILLED
    assert (
        await st.orders.on_venue_update(late.model_copy(update={"client_order_id": "nope"})) is None
    )


# --- CB / kill switch и защитные выходы ----------------------------------------------


async def test_tripped_cb_blocks_entries_but_not_exits(db: sqlite3.Connection) -> None:
    st, clock = stack(db)
    await process(st, clock, await classic_signal(db))
    clock.advance(60_000)
    buy = await classic_signal(db, now_ms=clock.now, bar_ts=clock.now)
    v_buy, d_buy = await decide(st, clock, buy)
    assert v_buy.approved
    await st.breaker.trip(CBTrigger.MANUAL, "test")
    with pytest.raises(ExecutionRefusedError, match="TRIPPED"):
        await st.orders.handle_verdict(v_buy, buy, decision_id=d_buy)
    close = await classic_signal(db, SignalAction.CLOSE, now_ms=clock.now, bar_ts=clock.now + 1)
    res = await process(st, clock, close)  # R2 пропускает CLOSE при TRIPPED
    assert res.verdict.approved and res.order is not None and res.order.state is OrderState.FILLED
    assert st.orders.positions.open("PAPER") == []


async def test_stop_loss_fires_even_when_killed(db: sqlite3.Connection) -> None:
    st, clock = stack(db)
    sig = await classic_signal(db)
    await process(st, clock, sig)
    [pos] = st.orders.positions.open("PAPER")
    db.execute(
        "UPDATE positions SET stop_loss = ?, take_profit = ?",
        (str(pos.avg_price * Decimal("0.9")), str(pos.avg_price * 2)),
    )
    db.commit()
    await st.kill_switch.engage(actor="test", flatten=False)
    assert await st.orders.check_protective_exits({"BTC/USDT": pos.avg_price}) == []
    [exit_order] = await st.orders.check_protective_exits(
        {"BTC/USDT": pos.avg_price * Decimal("0.8")}
    )
    assert exit_order.state is OrderState.FILLED and exit_order.side == "SELL"
    assert PositionManager(db).open("PAPER") == []
    strat = db.execute(
        "SELECT strategy_version, risk_verdict FROM decision_audit WHERE order_id = ?",
        (exit_order.id,),
    ).fetchone()
    assert strat == ("system:sl", "APPROVED")


async def test_take_profit_on_short(db: sqlite3.Connection) -> None:
    st, clock = stack(db)
    await process(st, clock, await classic_signal(db, SignalAction.SELL))
    [pos] = st.orders.positions.open("PAPER")
    assert pos.qty < 0
    db.execute("UPDATE positions SET take_profit = ?", (str(pos.avg_price * Decimal("0.95")),))
    db.commit()
    [o] = await st.orders.check_protective_exits({"BTC/USDT": pos.avg_price * Decimal("0.9")})
    assert (
        o.side == "BUY"
        and "system:tp"
        in db.execute(
            "SELECT strategy_version FROM decision_audit WHERE order_id=?", (o.id,)
        ).fetchone()
    )
    assert audit_events(db, "ORDER_DUPLICATE_SUPPRESSED") == []
