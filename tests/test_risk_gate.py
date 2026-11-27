"""Risk Gate: каждый чек R1–R18, порядок, отсутствие short-circuit, sizing, REDUCED, fail-closed."""

from datetime import timedelta
from decimal import Decimal

import pytest

from augur.adapters.base import MarketStatus
from augur.config.settings import RiskLimits, TradingMode
from augur.fusion.contract import RiskFlag
from augur.risk.gate import CHECK_NAMES, RiskGate, intent_key
from augur.risk.models import RiskVerdict
from augur.strategies.base import SignalAction
from tests._risk import NOW, make_ctx, make_signal


def _gate(**limits: object) -> RiskGate:
    return RiskGate(RiskLimits(**limits))  # type: ignore[arg-type]


def _failed(v: RiskVerdict) -> set[str]:
    return {c.name for c in v.failed}


def _check(v: RiskVerdict, name: str) -> tuple[bool, str | None]:
    c = next(c for c in v.checks if c.name == name)
    return c.passed, c.reason


async def test_happy_path_all_checks_pass_in_fixed_order() -> None:
    v = await _gate().evaluate(make_signal(), make_ctx())
    assert v.approved and v.risk_verdict == "APPROVED" and v.risk_reason is None
    assert tuple(c.name for c in v.checks) == CHECK_NAMES
    assert len(CHECK_NAMES) == 18
    # без стопа: 2% equity / price = 10000 * 0.02 / 100
    assert v.sized_qty == Decimal("2.000") and v.sized_notional == Decimal("200.000")
    assert v.mode == TradingMode.PAPER and v.evaluated_at == NOW


def test_no_short_circuit_reports_every_failure() -> None:
    sig = make_signal(SignalAction.HOLD, expires_in=timedelta(seconds=-30))
    ctx = make_ctx(
        mode=TradingMode.SIGNAL_ONLY,
        cb_state="TRIPPED",
        spread_bps=Decimal("41.2"),
        market_status=MarketStatus.HALTED,
    )
    v = _gate().evaluate_sync(sig, ctx)
    assert not v.approved and v.risk_verdict == "REJECTED"
    assert {
        "mode_allows_trading",
        "circuit_breaker_armed",
        "signal_ttl",
        "action_actionable",
        "market_open",
        "spread",
        "position_size",
    } <= _failed(v)
    assert v.sized_qty is None and v.sized_notional is None
    assert v.risk_reason is not None and "spread: spread_bps above limit" in v.risk_reason


@pytest.mark.parametrize("mode", [TradingMode.ANALYSIS_ONLY, TradingMode.SIGNAL_ONLY])
def test_r1_mode_not_trading(mode: TradingMode) -> None:
    assert "mode_allows_trading" in _failed(
        _gate().evaluate_sync(make_signal(), make_ctx(mode=mode))
    )


@pytest.mark.parametrize("state", ["TRIPPED", "KILLED"])
def test_r2_cb_blocks_entries(state: str) -> None:
    v = _gate().evaluate_sync(make_signal(), make_ctx(cb_state=state))
    assert _failed(v) == {"circuit_breaker_armed"}


def test_r2_tripped_cb_allows_close_but_killed_does_not() -> None:
    pos = {"BTC/USDT": Decimal("1.5")}
    close = make_signal(SignalAction.CLOSE)
    tripped = _gate().evaluate_sync(close, make_ctx(cb_state="TRIPPED", positions=pos))
    assert tripped.approved and tripped.sized_qty == Decimal("1.5")
    killed = _gate().evaluate_sync(close, make_ctx(cb_state="KILLED", positions=pos))
    assert "circuit_breaker_armed" in _failed(killed)


def test_r3_ttl_boundary_now_equal_expires_is_expired() -> None:
    v = _gate().evaluate_sync(make_signal(expires_in=timedelta(0)), make_ctx())
    assert _failed(v) == {"signal_ttl"}


@pytest.mark.parametrize("action", [SignalAction.HOLD, SignalAction.NO_SIGNAL])
def test_r4_hold_and_no_signal_rejected(action: SignalAction) -> None:
    v = _gate().evaluate_sync(make_signal(action), make_ctx())
    assert _failed(v) == {"action_actionable", "position_size", "concentration", "margin_available"}


def test_r5_unknown_orders() -> None:
    ctx = make_ctx(unknown_order_symbols=frozenset({"BTC/USDT"}))
    assert _failed(_gate().evaluate_sync(make_signal(), ctx)) == {"no_unknown_orders"}


@pytest.mark.parametrize("status", [MarketStatus.CLOSED, MarketStatus.HALTED, None])
def test_r6_market_not_open(status: MarketStatus | None) -> None:
    v = _gate().evaluate_sync(make_signal(), make_ctx(market_status=status))
    assert _failed(v) == {"market_open"}


@pytest.mark.parametrize("z", [Decimal("4.0"), Decimal("-5"), None])
def test_r7_abnormal_volatility(z: Decimal | None) -> None:
    v = _gate().evaluate_sync(make_signal(), make_ctx(volatility_zscore=z))
    assert _failed(v) == {"abnormal_volatility"}


def test_r7_not_applied_to_close() -> None:
    ctx = make_ctx(volatility_zscore=Decimal("9"), positions={"BTC/USDT": Decimal("1")})
    assert _gate().evaluate_sync(make_signal(SignalAction.CLOSE), ctx).approved


def test_r8_spread_limit_inclusive() -> None:
    assert _gate().evaluate_sync(make_signal(), make_ctx(spread_bps=Decimal("30"))).approved
    v = _gate().evaluate_sync(make_signal(), make_ctx(spread_bps=Decimal("30.01")))
    assert _failed(v) == {"spread"}


def test_r9_liquidity() -> None:
    v = _gate().evaluate_sync(make_signal(), make_ctx(liquidity_quote=Decimal("99999")))
    assert _failed(v) == {"liquidity"}
    assert _failed(_gate().evaluate_sync(make_signal(), make_ctx(liquidity_quote=None))) == {
        "liquidity"
    }


def test_r10_daily_loss_blocks_entry_not_exit() -> None:
    # лимит: -0.03 * 10000 = -300; day_pnl должен быть строго больше
    v = _gate().evaluate_sync(make_signal(), make_ctx(day_pnl=Decimal("-300")))
    assert _failed(v) == {"daily_loss"}
    assert _gate().evaluate_sync(make_signal(), make_ctx(day_pnl=Decimal("-299.99"))).approved
    ctx = make_ctx(day_pnl=Decimal("-1000"), positions={"BTC/USDT": Decimal("1")})
    assert _gate().evaluate_sync(make_signal(SignalAction.CLOSE), ctx).approved


def test_r10_no_snapshot_fails_closed() -> None:
    v = _gate().evaluate_sync(make_signal(), make_ctx(day_pnl=None, equity_start_of_day=None))
    assert _failed(v) == {"daily_loss"}


def test_r11_max_open_positions() -> None:
    full = {f"S{i}": Decimal("1") for i in range(5)}
    v = _gate().evaluate_sync(make_signal(), make_ctx(positions=full))
    assert _failed(v) == {"max_open_positions"}
    # уже есть позиция по символу — счётчик не растёт
    full_with_btc = {**{f"S{i}": Decimal("1") for i in range(4)}, "BTC/USDT": Decimal("0.5")}
    assert _gate().evaluate_sync(make_signal(), make_ctx(positions=full_with_btc)).approved
    # нулевые позиции не считаются открытыми
    zeros = {f"S{i}": Decimal("0") for i in range(5)}
    assert _gate().evaluate_sync(make_signal(), make_ctx(positions=zeros)).approved


def test_r12_concentration_includes_existing_exposure() -> None:
    # текущая 24 * 100 = 2400 + новые 200 → 0.26 > 0.25
    ctx = make_ctx(positions={"BTC/USDT": Decimal("24")})
    v = _gate().evaluate_sync(make_signal(), ctx)
    assert _failed(v) == {"concentration"}
    assert (
        _gate()
        .evaluate_sync(make_signal(), make_ctx(positions={"BTC/USDT": Decimal("23")}))
        .approved
    )


def test_r13_below_min_notional() -> None:
    # equity 400 → 2% = 8 USDT < min_notional 10
    ctx = make_ctx(equity=Decimal("400"), cash=Decimal("400"), equity_start_of_day=Decimal("400"))
    v = _gate().evaluate_sync(make_signal(), ctx)
    assert _failed(v) == {"position_size"}
    passed, reason = _check(v, "position_size")
    assert not passed and reason is not None and "min_notional" in reason


def test_r13_custom_sizer_cannot_exceed_cap() -> None:
    class Greedy:
        def size(self, *args: object) -> Decimal:
            return Decimal("1000")

    gate = RiskGate(RiskLimits(), sizer=Greedy())
    v = gate.evaluate_sync(make_signal(), make_ctx())
    assert v.approved and v.sized_qty == Decimal("2.000")  # Gate режет до max_position_pct


def test_r14_margin() -> None:
    v = _gate().evaluate_sync(
        make_signal(), make_ctx(cash=Decimal("250"), margin_used=Decimal("100"))
    )
    assert _failed(v) == {"margin_available"}


def test_r15_blocking_code_and_high_severity() -> None:
    blocked = make_signal(flags=[RiskFlag(code="news_halt", severity="low")])
    assert _failed(_gate().evaluate_sync(blocked, make_ctx())) == {"risk_flags"}
    high = make_signal(flags=[RiskFlag(code="anything", severity="high")])
    assert _failed(_gate().evaluate_sync(high, make_ctx())) == {"risk_flags"}
    medium = make_signal(flags=[RiskFlag(code="anything", severity="medium")])
    assert _gate().evaluate_sync(medium, make_ctx()).approved


def test_r15_by_mode_fusion_contrarian_only_in_micro_live() -> None:
    sig = make_signal(flags=[RiskFlag(code="fusion_contrarian", severity="medium")])
    assert _gate().evaluate_sync(sig, make_ctx()).approved
    v = _gate().evaluate_sync(sig, make_ctx(mode=TradingMode.MICRO_LIVE))
    assert _failed(v) == {"risk_flags"}


def test_r15_high_flag_does_not_block_close() -> None:
    sig = make_signal(SignalAction.CLOSE, flags=[RiskFlag(code="news_halt", severity="high")])
    ctx = make_ctx(positions={"BTC/USDT": Decimal("-2")})
    v = _gate().evaluate_sync(sig, ctx)
    assert v.approved and v.sized_qty == Decimal("2")


def test_r16_news_pause_symbol_and_strategy() -> None:
    ctx = make_ctx(news_paused_symbols=frozenset({"BTC/USDT"}))
    assert _failed(_gate().evaluate_sync(make_signal(), ctx)) == {"news_pause"}
    ctx = make_ctx(
        news_paused_strategies=frozenset({"sma_cross@1.0.0"}),
        signal_strategies=frozenset({"sma_cross@1.0.0"}),
    )
    assert _failed(_gate().evaluate_sync(make_signal(), ctx)) == {"news_pause"}


def test_r17_duplicate_intent() -> None:
    sig = make_signal()
    ctx = make_ctx(active_intents=frozenset({intent_key(sig.signal_id, "BUY")}))
    assert _failed(_gate().evaluate_sync(sig, ctx)) == {"duplicate_intent"}
    other_side = make_ctx(active_intents=frozenset({intent_key(sig.signal_id, "SELL")}))
    assert _gate().evaluate_sync(sig, other_side).approved


def test_r18_micro_live_cap_sizes_down() -> None:
    v = _gate().evaluate_sync(make_signal(), make_ctx(mode=TradingMode.MICRO_LIVE))
    assert v.approved
    assert v.sized_qty == Decimal("0.200") and v.sized_notional == Decimal("20.000")


def test_r18_micro_cap_below_min_notional_rejects() -> None:
    v = _gate(micro_live_max_notional=Decimal("5")).evaluate_sync(
        make_signal(), make_ctx(mode=TradingMode.MICRO_LIVE)
    )
    assert _failed(v) == {"position_size"}


def test_fixed_fractional_with_stop() -> None:
    # риск 0.5% * 10000 = 50; расстояние до стопа 50 → qty 1
    v = _gate().evaluate_sync(make_signal(stop_loss=Decimal("50")), make_ctx())
    assert v.approved and v.sized_qty == Decimal("1.000")
    # SELL: стоп выше цены
    v = _gate().evaluate_sync(make_signal(SignalAction.SELL, stop_loss=Decimal("150")), make_ctx())
    assert v.approved and v.sized_qty == Decimal("1.000")


def test_stop_on_wrong_side_rejects() -> None:
    v = _gate().evaluate_sync(make_signal(stop_loss=Decimal("101")), make_ctx())
    assert _failed(v) == {"position_size"}
    assert _check(v, "position_size")[1] == (
        "sized_qty is zero; qty below min_qty 0.001; notional below min_notional 10"
    )


def test_soft_flag_reduces_and_marks_reduced() -> None:
    sig = make_signal(flags=[RiskFlag(code="news_mixed", severity="medium")])
    v = _gate().evaluate_sync(sig, make_ctx())
    assert v.approved and v.risk_verdict == "REDUCED"
    assert v.sized_qty == Decimal("1.000")
    assert v.risk_reason is not None and "news_mixed" in v.risk_reason


def test_reduce_takes_fraction_of_position() -> None:
    ctx = make_ctx(positions={"BTC/USDT": Decimal("1.5")})
    v = _gate().evaluate_sync(make_signal(SignalAction.REDUCE), ctx)
    assert v.approved and v.sized_qty == Decimal("0.750")


def test_close_without_position_rejected() -> None:
    v = _gate().evaluate_sync(make_signal(SignalAction.CLOSE), make_ctx())
    assert _failed(v) == {"position_size"}


def test_missing_market_data_fails_closed() -> None:
    v = _gate().evaluate_sync(make_signal(), make_ctx(instrument=None, price=None, equity=None))
    assert {"position_size", "concentration", "margin_available"} <= _failed(v)


def test_deterministic_given_same_inputs() -> None:
    gate = RiskGate(RiskLimits(), id_factory=lambda: "V")
    sig, ctx = make_signal(), make_ctx()
    assert gate.evaluate_sync(sig, ctx) == gate.evaluate_sync(sig, ctx)


@pytest.mark.parametrize(
    "bad",
    [
        {"risk_flag_multiplier": Decimal("1")},  # soft-флаг обязан уменьшать
        {"risk_flag_multiplier": Decimal("0")},
        {"reduce_fraction": Decimal("1.5")},
        {"risk_per_trade_pct": Decimal("0")},
    ],
)
def test_limits_validated(bad: dict[str, Decimal]) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RiskLimits(**bad)  # type: ignore[arg-type]
