"""Execution: state machine, client_order_id, CostModel, PaperAdapter с издержками,
ResilientAdapter, PositionManager / PortfolioTracker, миграция 0005."""

import sqlite3
from decimal import Decimal

import pytest

from augur.adapters import (
    AccountState,
    CallPolicy,
    FatalAdapterError,
    OrderAck,
    OrderRequest,
    OrderState,
    PaperAdapter,
    RejectedError,
    ResilientAdapter,
    RetryableError,
    VenuePosition,
)
from augur.backtest.costs import CostModel
from augur.execution.idempotency import CLIENT_ORDER_ID_RE, make_client_order_id
from augur.execution.positions import PortfolioTracker, PositionManager
from augur.execution.state_machine import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    IllegalTransitionError,
    check_transition,
    path_to,
)

S = OrderState

# --- state machine (ARCH §7.3) --------------------------------------------------------


def test_transition_table_matches_spec() -> None:
    assert ALLOWED_TRANSITIONS == {
        S.PENDING: {S.SUBMITTED, S.REJECTED, S.UNKNOWN},
        S.SUBMITTED: {S.PARTIAL, S.FILLED, S.REJECTED, S.CANCELLED, S.UNKNOWN},
        S.PARTIAL: {S.PARTIAL, S.FILLED, S.CANCELLED, S.UNKNOWN},
        S.UNKNOWN: {S.SUBMITTED, S.PARTIAL, S.FILLED, S.REJECTED, S.CANCELLED},
        S.FILLED: set(),
        S.REJECTED: set(),
        S.CANCELLED: set(),
    }
    assert TERMINAL_STATES == {S.FILLED, S.REJECTED, S.CANCELLED}


@pytest.mark.parametrize(
    ("a", "b"), [(S.PENDING, S.FILLED), (S.FILLED, S.CANCELLED), (S.PARTIAL, S.REJECTED)]
)
def test_illegal_transitions(a: OrderState, b: OrderState) -> None:
    with pytest.raises(IllegalTransitionError):
        check_transition(a, b)


def test_path_to_inserts_intermediate_states() -> None:
    assert path_to(S.PENDING, S.FILLED) == [S.SUBMITTED, S.FILLED]
    assert path_to(S.PENDING, S.PARTIAL) == [S.SUBMITTED, S.PARTIAL]
    assert path_to(S.PENDING, S.CANCELLED) == [S.SUBMITTED, S.CANCELLED]
    assert path_to(S.UNKNOWN, S.FILLED) == [S.FILLED]
    assert path_to(S.PARTIAL, S.PARTIAL) == [S.PARTIAL]
    assert path_to(S.SUBMITTED, S.SUBMITTED) == []
    with pytest.raises(IllegalTransitionError):
        path_to(S.FILLED, S.CANCELLED)


# --- client_order_id ------------------------------------------------------------------


def test_client_order_id_is_deterministic_and_fits_venue_limits() -> None:
    a = make_client_order_id("01HK18HSK8BTJRZGTVMM1MHZ1M")
    assert a == make_client_order_id("01HK18HSK8BTJRZGTVMM1MHZ1M", "entry", 0)
    assert len(a) == 24 and CLIENT_ORDER_ID_RE.fullmatch(a)
    assert a != make_client_order_id("01HK18HSK8BTJRZGTVMM1MHZ1M", "entry", 1)
    assert a != make_client_order_id("01HK18HSK8BTJRZGTVMM1MHZ1M", "sl", 0)
    assert a != make_client_order_id("01HK18HSK8BTJRZGTVMM1MHZ1N")


@pytest.mark.parametrize(
    ("sid", "leg", "attempt"), [("", "entry", 0), ("x", "bogus", 0), ("x", "entry", -1)]
)
def test_client_order_id_rejects_bad_input(sid: str, leg: str, attempt: int) -> None:
    with pytest.raises(ValueError):
        make_client_order_id(sid, leg, attempt)


# --- CostModel / PaperAdapter ---------------------------------------------------------


def test_cost_model_decimal_and_float_agree() -> None:
    c = CostModel(commission_pct=Decimal("0.1"), slippage_pct=Decimal("0.05"))
    assert c.fill_price("BUY", Decimal(100)) == Decimal("100.0500")
    assert c.fill_price("SELL", Decimal(100)) == Decimal("99.9500")
    assert c.fee(Decimal(-1000)) == Decimal("1.000")
    assert c.fill_price_f("BUY", 100.0) == pytest.approx(100.05)
    assert c.fee_f(1000.0) == pytest.approx(1.0)
    assert CostModel.from_floats(0.1, 0.05) == c


def _req(coid: str, side: str = "BUY", qty: str = "1", **kw: object) -> OrderRequest:
    return OrderRequest(
        client_order_id=coid,
        symbol="X",
        side=side,
        type="MARKET",
        qty=Decimal(qty),
        **kw,  # type: ignore[arg-type]
    )


async def test_paper_applies_slippage_and_fee() -> None:
    costs = CostModel(commission_pct=Decimal("0.1"), slippage_pct=Decimal("0.05"))
    a = PaperAdapter(clock_ms=1_795_000_000_000, costs=costs)
    tick = await a.get_latest("X")
    ack = await a.place_order(_req("c1", qty="2"))
    price = (tick.ask * Decimal("1.0005")).quantize(Decimal("0.00000001"))
    assert ack.avg_price == price
    fee = (price * 2 * Decimal("0.001")).quantize(Decimal("0.00000001"))
    assert ack.fee == fee and ack.fee_ccy == "USDT"
    [fill] = ack.fills
    assert (fill.qty, fill.price, fill.fee) == (Decimal(2), price, fee)
    acct = await a.get_account_state()
    assert acct.cash == Decimal(10000) - (price * 2).quantize(Decimal("0.00000001")) - fee
    assert tick.volume is not None and tick.volume > 0  # «24h-объём» для R9


async def test_paper_reduce_only_cannot_open_or_flip() -> None:
    a = PaperAdapter(clock_ms=1_795_000_000_000)
    with pytest.raises(RejectedError, match="reduce_only"):
        await a.place_order(_req("r0", "SELL", reduce_only=True))
    await a.place_order(_req("b1", "BUY", "1"))
    with pytest.raises(RejectedError, match="reduce_only"):
        await a.place_order(_req("r1", "SELL", "2", reduce_only=True))
    await a.place_order(_req("r2", "SELL", "1", reduce_only=True))
    assert await a.get_positions() == []


async def test_paper_restore_state() -> None:
    a = PaperAdapter(clock_ms=1_795_000_000_000)
    a.restore(
        cash=Decimal(500),
        positions=[VenuePosition(symbol="X", qty=Decimal(3), avg_price=Decimal(90))],
    )
    [p] = await a.get_positions()
    assert (p.qty, p.avg_price) == (Decimal(3), Decimal(90))
    assert (await a.get_account_state()).cash == Decimal(500)


# --- ResilientAdapter -----------------------------------------------------------------


class _Flaky(PaperAdapter):
    def __init__(self) -> None:
        super().__init__(clock_ms=1_795_000_000_000)
        self.place_calls = 0
        self.get_calls = 0
        self.get_failures = 0
        self.fatal = False

    async def place_order(self, req: OrderRequest) -> OrderAck:
        self.place_calls += 1
        raise RetryableError("network")

    async def get_order(self, client_order_id: str, symbol: str) -> OrderAck | None:
        self.get_calls += 1
        if self.fatal:
            raise FatalAdapterError("auth")
        if self.get_failures:
            self.get_failures -= 1
            raise RetryableError("5xx")
        return None


async def _nosleep(_: float) -> None:
    return None


async def test_resilient_never_retries_place_order() -> None:
    inner, errors = _Flaky(), []
    r = ResilientAdapter(inner, CallPolicy(max_retries=3), on_error=errors.append, sleep=_nosleep)
    with pytest.raises(RetryableError):
        await r.place_order(_req("x"))
    assert inner.place_calls == 1 and len(errors) == 1


async def test_resilient_retries_idempotent_reads_and_reports_errors() -> None:
    inner, errors = _Flaky(), []
    inner.get_failures = 2
    r = ResilientAdapter(inner, CallPolicy(max_retries=3), on_error=errors.append, sleep=_nosleep)
    assert await r.get_order("x", "X") is None
    assert inner.get_calls == 3 and len(errors) == 2
    inner.fatal = True
    with pytest.raises(FatalAdapterError):
        await r.get_order("x", "X")
    assert isinstance(errors[-1], FatalAdapterError)


async def test_resilient_does_not_report_rejections() -> None:
    errors: list[BaseException] = []
    r = ResilientAdapter(PaperAdapter(clock_ms=1_795_000_000_000), on_error=errors.append)
    with pytest.raises(RejectedError):
        await r.place_order(_req("x", "SELL", reduce_only=True))
    assert errors == []
    assert r.venue == "paper"


# --- позиции и снимки -----------------------------------------------------------------


def _fill(
    pm: PositionManager, side: str, qty: str, price: str, fee: str = "0", ts: int = 1
) -> object:
    return pm.apply_fill(
        venue="paper", symbol="X", mode="PAPER", side=side, qty=Decimal(qty),  # type: ignore[arg-type]
        price=Decimal(price), fee=Decimal(fee), ts=ts,
        stop_loss=Decimal("90"), take_profit=Decimal("120"),
    )  # fmt: skip


def test_position_open_add_reduce_flip_close(db: sqlite3.Connection) -> None:
    pm = PositionManager(db)
    e = _fill(pm, "BUY", "1", "100", fee="0.1")
    assert e.position.qty == 1 and e.realized_pnl == Decimal("-0.1") and not e.trade_closed  # type: ignore[attr-defined]
    assert e.position.stop_loss == Decimal(90) and e.position.opened_ts == 1  # type: ignore[attr-defined]
    e = _fill(pm, "BUY", "1", "110", ts=2)
    assert e.position.avg_price == Decimal("105") and e.position.opened_ts == 1  # type: ignore[attr-defined]
    e = _fill(pm, "SELL", "1", "115", fee="0.2", ts=3)  # частичное закрытие: +10 − 0.2
    assert e.realized_pnl == Decimal("9.8") and e.closed_qty == 1 and not e.trade_closed  # type: ignore[attr-defined]
    assert e.position.avg_price == Decimal("105")  # type: ignore[attr-defined]
    e = _fill(pm, "SELL", "3", "100", ts=4)  # закрыть 1 (−5) и открыть шорт 2 по 100
    assert e.realized_pnl == Decimal("-5") and e.trade_closed  # type: ignore[attr-defined]
    p = e.position  # type: ignore[attr-defined]
    assert (p.qty, p.avg_price, p.opened_ts) == (Decimal(-2), Decimal(100), 4)
    e = _fill(pm, "BUY", "2", "90", ts=5)  # шорт в плюс: +20
    assert e.realized_pnl == Decimal(20) and e.trade_closed  # type: ignore[attr-defined]
    p = e.position  # type: ignore[attr-defined]
    assert (p.qty, p.avg_price, p.stop_loss, p.opened_ts) == (Decimal(0), Decimal(0), None, None)
    assert p.realized_pnl == Decimal("-0.1") + Decimal("9.8") - 5 + 20
    assert pm.open("PAPER") == []


def _acct(equity: str, ts: int) -> AccountState:
    return AccountState(
        equity=Decimal(equity), cash=Decimal(equity), margin_used=Decimal(0), currency="USDT", ts=ts
    )


DAY = 86_400_000


def test_snapshot_day_pnl_peak_and_drawdown(db: sqlite3.Connection) -> None:
    t = PortfolioTracker(db)
    d0 = 20_000 * DAY
    s = t.snapshot("PAPER", _acct("10000", d0 + 1), [], ts=d0 + 1)
    assert (s.day_pnl, s.peak_equity, s.drawdown_pct) == (Decimal(0), Decimal(10000), Decimal(0))
    s = t.snapshot("PAPER", _acct("11000", d0 + 2), [], ts=d0 + 2)
    assert (s.day_pnl, s.peak_equity) == (Decimal(1000), Decimal(11000))
    s = t.snapshot("PAPER", _acct("9900", d0 + DAY + 5), [], ts=d0 + DAY + 5)  # следующий день
    assert s.day_pnl == Decimal(-1100)  # от equity на конец вчера (11000)
    assert s.peak_equity == Decimal(11000) and s.drawdown_pct == Decimal("0.1")
    assert t.latest("PAPER") == s


def test_migration_0005_fills_dedup(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO signals(id, kind, symbol, action, strength, source, config_hash, "
        "code_version, created_ts) VALUES ('s', 'final', 'X', 'BUY', 1, 'classic', 'c', 'v', 0)"
    )
    db.execute(
        "INSERT INTO orders(id, client_order_id, signal_id, risk_verdict_id, venue, symbol, side, "
        "type, qty, state, mode, created_ts, updated_ts) "
        "VALUES ('o', 'c', 's', 'v', 'paper', 'X', 'BUY', 'MARKET', '1', 'PENDING', 'PAPER', 0, 0)"
    )
    fill = "INSERT INTO fills(id, order_id, venue_fill_id, qty, price, ts) VALUES (?,?,?,'1','1',0)"
    db.execute(fill, ("f1", "o", "v1"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(fill, ("f2", "o", "v1"))  # тот же venue_fill_id
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(fill, ("f3", "zz", "v2"))  # FK на orders
