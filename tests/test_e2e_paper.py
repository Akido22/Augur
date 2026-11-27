"""E2E PAPER (ARCH §14 этап 6): сигнал → Risk Gate → decision_audit → ордер → fill → позиция."""

import json
import sqlite3
from decimal import Decimal

from augur.adapters.base import OrderState
from augur.strategies.base import SignalAction
from tests._exec import audit_events, classic_signal, process, stack


async def test_buy_signal_is_executed_end_to_end(db: sqlite3.Connection) -> None:
    st, clock = stack(db)
    sig = await classic_signal(db)
    res = await process(st, clock, sig)
    assert res.verdict.approved, res.verdict.risk_reason
    order = res.order
    assert order is not None and order.state is OrderState.FILLED
    assert order.filled_qty == res.verdict.sized_qty and order.fee > 0
    [pos] = st.orders.positions.open("PAPER")
    assert pos.qty == order.filled_qty
    da = db.execute(
        "SELECT order_id, execution_result FROM decision_audit WHERE id=?", (res.decision.id,)
    ).fetchone()
    assert da[0] == order.id
    assert json.loads(da[1])["state"] == "FILLED"
    assert db.execute("SELECT status FROM signals WHERE id=?", (sig.signal_id,)).fetchone() == (
        "EXECUTED",
    )
    assert [e for e in audit_events(db) if e.startswith(("RISK", "ORDER"))] == [
        "RISK_APPROVED",
        "ORDER_UPDATED",  # PENDING
        "ORDER_UPDATED",  # fill
        "ORDER_SUBMITTED",
        "ORDER_UPDATED",  # FILLED
    ]
    assert Decimal(str(db.execute("SELECT COUNT(*) FROM fills").fetchone()[0])) == 1
    # повторная обработка того же решения: дубль подавлен, сделки нет
    again = await st.orders.handle_verdict(res.verdict, sig, decision_id=res.decision.id)
    assert again is not None and again.id == order.id
    assert audit_events(db, "ORDER_DUPLICATE_SUPPRESSED") == ["ORDER_DUPLICATE_SUPPRESSED"]
    assert db.execute("SELECT COUNT(*) FROM orders").fetchone() == (1,)
    [pos2] = await st.broker.get_positions()
    assert pos2.qty == pos.qty


async def test_close_signal_closes_position(db: sqlite3.Connection) -> None:
    st, clock = stack(db)
    await process(st, clock, await classic_signal(db))
    clock.advance(60_000)
    sig = await classic_signal(db, SignalAction.CLOSE, now_ms=clock.now, bar_ts=clock.now)
    res = await process(st, clock, sig)
    assert res.verdict.approved, res.verdict.risk_reason
    assert res.order is not None and res.order.side == "SELL"
    assert st.orders.positions.open("PAPER") == []
    assert await st.broker.get_positions() == []
