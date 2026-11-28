"""FULL_AUTO (0.11.0): promotion, утверждающий, перенос книги, исполнение без подтверждения."""

import json
import sqlite3
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from augur.config.settings import ModeSettings, Settings, TradingMode
from augur.db.audit import append_audit
from augur.execution import (
    ModeChangeError,
    ModeController,
    ModeStore,
    OrderRepository,
    build_live_stack,
    live_book_mode,
)
from augur.execution.positions import PositionManager
from augur.execution.promotion import promotion_status
from augur.risk.post_trade import CBTrigger
from augur.strategies.base import SignalAction
from tests._exec import APP, Clock, audit_events, classic_signal
from tests._live import FakeLiveVenue, live_settings, live_stack, process_live
from tests.test_signal_ready import T0

H = 3_600_000
VENUE = "ccxt:fake"


def full_settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "trading_mode": TradingMode.FULL_AUTO,
        "max_allowed_mode": TradingMode.FULL_AUTO,
        "telegram_bot_token": SecretStr("123:abc"),
        "telegram_chat_id": "42",
        "mode": ModeSettings(min_micro_trades=3, min_compared_orders=3),
    }
    base.update(over)
    return live_settings(**base)


async def _enter_micro(db: sqlite3.Connection, cfg: Settings, clock: Clock) -> ModeController:
    ctl = ModeController(db, settings=cfg, clock=clock)
    t = await ctl.request_change(TradingMode.MICRO_LIVE, actor="t")
    ctl.confirm_change(t.ticket_id, t.code, actor="t")
    return ctl


async def _micro_fills(
    db: sqlite3.Connection,
    n: int,
    *,
    ts: int,
    fill: str = "100.05",
    fee: str = "0.010005",
    with_bars: bool = True,
    state: str = "FILLED",
) -> list[str]:
    """n FILLED-ордеров MICRO_LIVE: BUY 0.1 по `fill`; close бара сигнала = 100."""
    repo = OrderRepository(db)
    ids = []
    for i in range(n):
        bar_ts = T0 + i * H
        sig = await classic_signal(db, now_ms=ts + i, bar_ts=bar_ts)
        if with_bars:
            db.execute(
                "INSERT OR REPLACE INTO ohlcv(venue, symbol, timeframe, ts_open, open, high, low, "
                "close, volume, ingested_ts) VALUES (?, 'BTC/USDT', '1h', ?, 100, 101, 99, 100, "
                "10, 0)",
                (VENUE, bar_ts),
            )
        order, _ = repo.insert_pending(
            client_order_id=f"c{ts}{i}",
            signal_id=sig.signal_id,
            risk_verdict_id="v",
            venue=VENUE,
            symbol="BTC/USDT",
            side="BUY",
            qty=Decimal("0.1"),
            mode="MICRO_LIVE",
            ts=ts + i,
        )
        db.execute(
            "UPDATE orders SET state = ?, filled_qty = '0.1', avg_fill_price = ?, fee = ? "
            "WHERE id = ?",
            (state, fill, fee, order.id),
        )
        ids.append(order.id)
    db.commit()
    return ids


# --- критерии promotion ----------------------------------------------------------------


async def test_promotion_ready_when_execution_matches_cost_model(db: sqlite3.Connection) -> None:
    clock, cfg = Clock(), full_settings()
    await _enter_micro(db, cfg, clock)
    st = promotion_status(db, cfg)
    assert not st.ok and any("trades 0/3" in r for r in st.reasons())
    await _micro_fills(db, 3, ts=clock.now)
    st = promotion_status(db, cfg)
    assert st.ok, st.reasons()
    assert st.compared_orders == 3 and st.mean_cost_excess_bps == Decimal(0)
    assert st.checks[0].model_price == Decimal("100.05")


async def test_promotion_blocked_by_cost_excess(db: sqlite3.Connection) -> None:
    clock, cfg = Clock(), full_settings()
    await _enter_micro(db, cfg, clock)
    await _micro_fills(db, 3, ts=clock.now, fill="101")  # ≈ +95 б.п. против модели
    st = promotion_status(db, cfg)
    assert not st.ok
    assert st.mean_cost_excess_bps is not None and st.mean_cost_excess_bps > 90
    assert any("mean cost excess" in r for r in st.reasons())


async def test_promotion_uncomparable_without_bars_fails_closed(db: sqlite3.Connection) -> None:
    clock, cfg = Clock(), full_settings()
    await _enter_micro(db, cfg, clock)
    await _micro_fills(db, 3, ts=clock.now, with_bars=False)
    st = promotion_status(db, cfg)
    assert st.micro_trades == 3 and st.compared_orders == 0 and st.uncomparable_orders == 3
    assert not st.ok


async def test_promotion_requires_alerts_and_no_active_orders(db: sqlite3.Connection) -> None:
    clock = Clock()
    cfg = full_settings(telegram_bot_token=None)
    await _enter_micro(db, cfg, clock)
    await _micro_fills(db, 3, ts=clock.now)
    await _micro_fills(db, 1, ts=clock.now + 10, state="SUBMITTED")
    reasons = promotion_status(db, cfg).reasons()
    assert any("alerts not configured" in r for r in reasons)
    assert any("1 active MICRO_LIVE orders" in r for r in reasons)
    relaxed = cfg.model_copy(
        update={
            "mode": ModeSettings(min_micro_trades=3, min_compared_orders=3, require_alerts=False)
        }
    )
    assert not any("alerts" in r for r in promotion_status(db, relaxed).reasons())


async def test_cb_trip_since_entry_blocks(db: sqlite3.Connection) -> None:
    clock, cfg = Clock(), full_settings()
    await _enter_micro(db, cfg, clock)
    await _micro_fills(db, 3, ts=clock.now)
    append_audit(db, actor="cb", event="CB_TRIPPED", severity="CRITICAL", ts=clock.now + 100)
    db.commit()
    assert promotion_status(db, cfg).cb_tripped_since_entry


# --- утверждение и перенос книги -------------------------------------------------------


def _open_position(db: sqlite3.Connection, mode: str, qty: str = "0.1") -> None:
    db.execute(
        "INSERT INTO positions(venue, symbol, mode, qty, avg_price, updated_ts) "
        "VALUES (?, 'BTC/USDT', ?, ?, '100', 0)",
        (VENUE, mode, qty),
    )
    db.execute(
        "INSERT INTO portfolio_snapshots(ts, mode, equity, cash, margin_used, day_pnl, "
        "peak_equity, drawdown_pct, positions_json) "
        "VALUES (1, ?, '1000', '990', '0', '0', '1000', '0', '[]')",
        (mode,),
    )
    db.commit()


async def test_full_auto_needs_approver_and_moves_book(db: sqlite3.Connection) -> None:
    clock, cfg = Clock(), full_settings()
    ctl = await _enter_micro(db, cfg, clock)
    await _micro_fills(db, 3, ts=clock.now)
    _open_position(db, "MICRO_LIVE")
    with pytest.raises(ModeChangeError, match="approved_by"):
        await ctl.request_change(TradingMode.FULL_AUTO, actor="t")
    ticket = await ctl.request_change(TradingMode.FULL_AUTO, actor="cli:t", approved_by="Artem")
    assert ticket.code_required and ticket.approved_by == "Artem"
    assert ctl.confirm_change(ticket.ticket_id, ticket.code, actor="cli:t") == (
        TradingMode.FULL_AUTO
    )
    payload = json.loads(
        db.execute(
            "SELECT payload_json FROM audit_log WHERE event = 'MODE_CHANGED' ORDER BY id DESC"
        ).fetchone()[0]
    )
    assert payload["approved_by"] == "Artem" and payload["promotion"]["compared_orders"] == 3
    assert payload["book_transfer"] == {"from": "MICRO_LIVE", "to": "FULL_AUTO", "positions": 1}
    pm = PositionManager(db)
    assert pm.get(VENUE, "BTC/USDT", "MICRO_LIVE") is None
    full = pm.get(VENUE, "BTC/USDT", "FULL_AUTO")
    assert full is not None and full.qty == Decimal("0.1")
    snap = db.execute("SELECT equity FROM portfolio_snapshots WHERE mode = 'FULL_AUTO'").fetchone()
    assert snap == ("1000",)

    # понижение FULL_AUTO → MICRO_LIVE возвращает книгу
    assert ctl.downgrade(TradingMode.MICRO_LIVE, actor="t", reason="back")
    assert pm.get(VENUE, "BTC/USDT", "MICRO_LIVE") is not None


async def test_book_transfer_refused_with_active_orders(db: sqlite3.Connection) -> None:
    store = ModeStore(db, configured_mode=TradingMode.FULL_AUTO)
    store.write(TradingMode.FULL_AUTO, actor="t", reason="setup", ts=1)
    db.commit()
    repo = OrderRepository(db)
    sig = await classic_signal(db)
    repo.insert_pending(
        client_order_id="x1",
        signal_id=sig.signal_id,
        risk_verdict_id="v",
        venue=VENUE,
        symbol="BTC/USDT",
        side="BUY",
        qty=Decimal("0.1"),
        mode="FULL_AUTO",
        ts=2,
    )
    db.commit()
    ctl = ModeController(db, settings=full_settings(), clock=Clock())
    with pytest.raises(ModeChangeError, match="active FULL_AUTO orders"):
        ctl.downgrade(TradingMode.MICRO_LIVE, actor="t", reason="x")
    db.rollback()
    assert ctl.downgrade(TradingMode.PAPER, actor="t", reason="emergency")  # вниз до PAPER — можно


async def test_live_book_after_cb_stays_full_auto(db: sqlite3.Connection) -> None:
    store = ModeStore(db, configured_mode=TradingMode.FULL_AUTO)
    assert live_book_mode(db, TradingMode.PAPER) == TradingMode.MICRO_LIVE
    _open_position(db, "FULL_AUTO")
    assert live_book_mode(db, TradingMode.PAPER) == TradingMode.FULL_AUTO
    assert live_book_mode(db, store.current()) == TradingMode.FULL_AUTO
    st = build_live_stack(
        db, full_settings(), app_version=APP, clock=Clock(), adapter=FakeLiveVenue(Clock())
    )
    assert st.orders.mode == TradingMode.FULL_AUTO


# --- исполнение ------------------------------------------------------------------------


async def test_full_auto_executes_without_manual_approval(db: sqlite3.Connection) -> None:
    clock = Clock()
    cfg = full_settings()
    ModeStore(db, configured_mode=TradingMode.FULL_AUTO).write(
        TradingMode.FULL_AUTO, actor="t", reason="setup", ts=clock.now
    )
    db.commit()
    st, clock, venue = live_stack(db, clock=clock, cfg=cfg)
    assert st.orders.mode == TradingMode.FULL_AUTO
    sig = await classic_signal(db, SignalAction.BUY, now_ms=clock.now)
    res = await process_live(st, clock, sig)
    assert res.verdict.approved, res.verdict
    assert res.order is not None and res.order.state.value == "FILLED"
    assert res.order.mode == "FULL_AUTO" and venue.place_calls == 1
    assert audit_events(db, "ORDER_APPROVAL_REQUIRED") == []
    row = db.execute(
        "SELECT mode, app_version, strategy_version, ml_model_version, llm_prompt_version "
        "FROM decision_audit WHERE order_id = ?",
        (res.order.id,),
    ).fetchone()
    assert row[0] == "FULL_AUTO" and all(row[1:])


async def test_cb_trip_in_full_auto_holds_paper(db: sqlite3.Connection) -> None:
    clock = Clock()
    cfg = full_settings()
    store = ModeStore(db, configured_mode=TradingMode.FULL_AUTO)
    store.write(TradingMode.FULL_AUTO, actor="t", reason="setup", ts=clock.now)
    db.commit()
    st, clock, _ = live_stack(db, clock=clock, cfg=cfg)
    sig = await classic_signal(db, SignalAction.BUY, now_ms=clock.now)
    assert (await process_live(st, clock, sig)).order is not None
    assert await st.breaker.trip(CBTrigger.MANUAL, "test")
    assert store.current() == TradingMode.PAPER  # PAPER-hold, позиция не закрыта
    # книга остаётся FULL_AUTO: SL/TP и kill switch работают с ней и после CB
    assert live_book_mode(db, store.current()) == TradingMode.FULL_AUTO
    pos = PositionManager(db).get(VENUE, "BTC/USDT", "FULL_AUTO")
    assert pos is not None and pos.qty > 0


async def test_kill_switch_flattens_full_auto_book(db: sqlite3.Connection) -> None:
    from augur.interfaces.control import engage_kill_switch

    clock = Clock()
    cfg = full_settings()
    ModeStore(db, configured_mode=TradingMode.FULL_AUTO).write(
        TradingMode.FULL_AUTO, actor="t", reason="setup", ts=clock.now
    )
    db.commit()
    st, clock, venue = live_stack(db, clock=clock, cfg=cfg)
    sig = await classic_signal(db, SignalAction.BUY, now_ms=clock.now)
    res = await process_live(st, clock, sig)
    assert res.order is not None and res.order.state.value == "FILLED"
    out = await engage_kill_switch(
        db, cfg, actor="t", flatten=True, clock=clock, live_adapter=venue
    )
    assert out.flatten_orders == 1 and not out.warnings
    pos = PositionManager(db).get(VENUE, "BTC/USDT", "FULL_AUTO")
    assert pos is not None and pos.qty == 0
