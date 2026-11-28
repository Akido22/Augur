"""ModeController (ARCH §1.4, §7.1): коды, один шаг, потолки конфига, CB, promotion, понижение."""

import json
import sqlite3
from decimal import Decimal

import pytest

from augur.config.settings import ModeSettings, TradingMode
from augur.db.audit import append_audit
from augur.execution import (
    InvalidModeCodeError,
    ModeChangeError,
    ModeController,
    ModeStore,
    OrderRepository,
)
from augur.execution.approval import first_order_confirmed
from augur.risk import RiskContextBuilder
from tests._exec import Clock, audit_events, classic_signal
from tests._live import live_settings

FULL = {"trading_mode": TradingMode.FULL_AUTO, "max_allowed_mode": TradingMode.FULL_AUTO}


def _ctl(db: sqlite3.Connection, clock: Clock | None = None, **over: object) -> ModeController:
    return ModeController(db, settings=live_settings(**over), clock=clock or Clock())


def _trip(db: sqlite3.Connection, state: str = "TRIPPED") -> None:
    db.execute(
        "INSERT INTO cb_state(id, state, reason, tripped_ts) VALUES (1, ?, 't', 0) "
        "ON CONFLICT(id) DO UPDATE SET state = excluded.state",
        (state,),
    )
    db.commit()


def test_config_alone_never_enables_live(db: sqlite3.Connection) -> None:
    """Конфиг MICRO_LIVE без `mode set` → PAPER (и в ModeStore, и в RiskContextBuilder)."""
    assert _ctl(db).current() == TradingMode.PAPER
    rcb = RiskContextBuilder(db, configured_mode=TradingMode.MICRO_LIVE)
    assert rcb.current_mode() == TradingMode.PAPER


async def test_escalation_with_one_time_code(db: sqlite3.Connection) -> None:
    ctl = _ctl(db)
    ticket = await ctl.request_change(TradingMode.MICRO_LIVE, actor="cli:test")
    assert ticket.code_required and len(ticket.code) == 6 and ticket.code.isdigit()
    req = db.execute(
        "SELECT payload_json FROM audit_log WHERE event = 'MODE_CHANGE_REQUESTED'"
    ).fetchone()
    assert ticket.code not in req[0]  # код в аудит не пишется
    assert ctl.current() == TradingMode.PAPER  # запрос режим не меняет
    assert ctl.confirm_change(ticket.ticket_id, ticket.code, actor="cli:test") == (
        TradingMode.MICRO_LIVE
    )
    payload = json.loads(
        db.execute("SELECT payload_json FROM audit_log WHERE event = 'MODE_CHANGED'").fetchone()[0]
    )
    assert payload["from"] == "PAPER" and payload["to"] == "MICRO_LIVE"
    assert not first_order_confirmed(db)


async def test_wrong_code_burns_ticket(db: sqlite3.Connection) -> None:
    ctl = _ctl(db)
    ticket = await ctl.request_change(TradingMode.MICRO_LIVE, actor="t")
    wrong = "000000" if ticket.code != "000000" else "111111"
    with pytest.raises(InvalidModeCodeError):
        ctl.confirm_change(ticket.ticket_id, wrong, actor="t")
    with pytest.raises(InvalidModeCodeError):  # повтор с верным кодом — тикет уже сгорел
        ctl.confirm_change(ticket.ticket_id, ticket.code, actor="t")
    assert ctl.current() == TradingMode.PAPER
    assert audit_events(db, "MODE_CHANGED") == []


async def test_code_expires(db: sqlite3.Connection) -> None:
    clock = Clock()
    ctl = _ctl(db, clock, mode=ModeSettings(code_ttl_s=120))
    ticket = await ctl.request_change(TradingMode.MICRO_LIVE, actor="t")
    clock.advance(121_000)
    with pytest.raises(InvalidModeCodeError):
        ctl.confirm_change(ticket.ticket_id, ticket.code, actor="t")
    assert ctl.current() == TradingMode.PAPER


async def test_one_step_at_a_time(db: sqlite3.Connection) -> None:
    ctl = _ctl(db, **FULL)
    with pytest.raises(ModeChangeError, match="one step"):
        await ctl.request_change(TradingMode.FULL_AUTO, actor="t")
    ctl.downgrade(TradingMode.SIGNAL_ONLY, actor="t", reason="test")
    with pytest.raises(ModeChangeError, match="one step"):
        await ctl.request_change(TradingMode.MICRO_LIVE, actor="t")
    ticket = await ctl.request_change(TradingMode.PAPER, actor="t")
    assert not ticket.code_required  # до PAPER код не обязателен (CLI подтверждает сам)


@pytest.mark.parametrize(
    "over",
    [
        {"trading_mode": TradingMode.PAPER, "max_allowed_mode": TradingMode.MICRO_LIVE},
        {"trading_mode": TradingMode.MICRO_LIVE, "max_allowed_mode": TradingMode.PAPER},
    ],
    ids=["trading_mode", "max_allowed_mode"],
)
async def test_config_ceilings(db: sqlite3.Connection, over: dict[str, TradingMode]) -> None:
    with pytest.raises(ModeChangeError, match="above settings"):
        await _ctl(db, **over).request_change(TradingMode.MICRO_LIVE, actor="t")


@pytest.mark.parametrize("state", ["TRIPPED", "KILLED"])
async def test_cb_blocks_escalation(db: sqlite3.Connection, state: str) -> None:
    _trip(db, state)
    with pytest.raises(ModeChangeError, match="circuit breaker"):
        await _ctl(db).request_change(TradingMode.MICRO_LIVE, actor="t")


async def test_cb_trip_between_request_and_confirm(db: sqlite3.Connection) -> None:
    ctl = _ctl(db)
    ticket = await ctl.request_change(TradingMode.MICRO_LIVE, actor="t")
    _trip(db)
    with pytest.raises(ModeChangeError, match="circuit breaker"):
        ctl.confirm_change(ticket.ticket_id, ticket.code, actor="t")
    assert ctl.current() == TradingMode.PAPER


async def test_preflight_failure_blocks_request(db: sqlite3.Connection) -> None:
    calls: list[TradingMode] = []

    async def preflight(target: TradingMode) -> None:
        calls.append(target)
        raise ModeChangeError("positions differ")

    ctl = ModeController(db, settings=live_settings(), clock=Clock(), preflight=preflight)
    with pytest.raises(ModeChangeError, match="positions differ"):
        await ctl.request_change(TradingMode.MICRO_LIVE, actor="t")
    assert calls == [TradingMode.MICRO_LIVE]
    assert audit_events(db, "MODE_CHANGE_REQUESTED") == []


async def _filled_micro_orders(db: sqlite3.Connection, n: int, ts: int) -> None:
    repo = OrderRepository(db)
    for i in range(n):
        sig = await classic_signal(db, now_ms=ts + i)
        order, _ = repo.insert_pending(
            client_order_id=f"c{i}",
            signal_id=sig.signal_id,
            risk_verdict_id="v",
            venue="ccxt:fake",
            symbol="BTC/USDT",
            side="BUY",
            qty=Decimal("0.1"),
            mode="MICRO_LIVE",
            ts=ts + i,
        )
        db.execute("UPDATE orders SET state = 'FILLED' WHERE id = ?", (order.id,))
    db.commit()


async def test_full_auto_requires_promotion(db: sqlite3.Connection) -> None:
    clock = Clock()
    ctl = _ctl(db, clock, mode=ModeSettings(min_micro_trades=3), **FULL)
    t = await ctl.request_change(TradingMode.MICRO_LIVE, actor="t")
    ctl.confirm_change(t.ticket_id, t.code, actor="t")
    with pytest.raises(ModeChangeError, match="promotion"):
        await ctl.request_change(TradingMode.FULL_AUTO, actor="t")
    await _filled_micro_orders(db, 3, clock.now)
    st = ctl.promotion_status()
    assert st.micro_trades == 3 and not st.cb_tripped_since_entry
    # 0.11.0: сделок мало для FULL_AUTO — нужна ещё сверка с CostModel и алерты
    # (успешный путь — tests/test_full_auto.py)
    assert not st.ok and any("CostModel" in r for r in st.reasons())
    with pytest.raises(ModeChangeError, match="CostModel"):
        await ctl.request_change(TradingMode.FULL_AUTO, actor="t", approved_by="a")


async def test_promotion_resets_after_cb_trip(db: sqlite3.Connection) -> None:
    clock = Clock()
    ctl = _ctl(db, clock, mode=ModeSettings(min_micro_trades=1), **FULL)
    t = await ctl.request_change(TradingMode.MICRO_LIVE, actor="t")
    ctl.confirm_change(t.ticket_id, t.code, actor="t")
    await _filled_micro_orders(db, 2, clock.now)
    append_audit(db, actor="cb", event="CB_TRIPPED", severity="CRITICAL", ts=clock.now)
    db.commit()
    st = ctl.promotion_status()
    assert st.cb_tripped_since_entry and not st.ok


async def test_downgrade_without_code(db: sqlite3.Connection) -> None:
    ctl = _ctl(db)
    t = await ctl.request_change(TradingMode.MICRO_LIVE, actor="t")
    ctl.confirm_change(t.ticket_id, t.code, actor="t")
    assert ctl.downgrade(TradingMode.PAPER, actor="cli:t", reason="enough")
    assert ctl.current() == TradingMode.PAPER
    with pytest.raises(ModeChangeError, match="not a downgrade"):
        ctl.downgrade(TradingMode.MICRO_LIVE, actor="t", reason="x")


async def test_store_still_refuses_escalation(db: sqlite3.Connection) -> None:
    store = ModeStore(db, configured_mode=TradingMode.MICRO_LIVE)
    with pytest.raises(PermissionError, match="ModeController"):
        store.set_mode(TradingMode.MICRO_LIVE, actor="t", reason="x", ts=0)


async def test_sync_with_config_downgrades_with_audit(db: sqlite3.Connection) -> None:
    ctl = _ctl(db)
    t = await ctl.request_change(TradingMode.MICRO_LIVE, actor="t")
    ctl.confirm_change(t.ticket_id, t.code, actor="t")
    lowered = ModeController(
        db, settings=live_settings(trading_mode=TradingMode.PAPER), clock=Clock()
    )
    assert lowered.sync_with_config() == TradingMode.PAPER
    assert db.execute("SELECT mode FROM mode_state").fetchone()[0] == "PAPER"
    last = db.execute(
        "SELECT payload_json FROM audit_log WHERE event = 'MODE_CHANGED' ORDER BY id DESC"
    ).fetchone()[0]
    assert "config ceiling" in last
