"""fuse(): правила §4.3 по порядку, golden-пример §4.6, инварианты §4.3 (hypothesis), verify()."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from augur.config.settings import FusionConfig
from augur.fusion.engine import FusionInvariantError, fuse, verify
from augur.strategies.base import SignalAction
from tests.fusion._fx import BAR, NOW, TS, kw, llm, ml, tech

CFG = FusionConfig()


def test_spec_golden_example_4_6() -> None:
    sig = fuse(ml(), llm(flags=[("exchange_incident", "medium")]), tech(), CFG, **kw())
    assert sig.action == SignalAction.HOLD and sig.reason == "below_entry_threshold"
    assert sig.strength == pytest.approx(0.0929, abs=1e-4)
    rules = sig.source_breakdown["rules_fired"]
    assert rules == ["llm_min_dampening", "medium_flag_x0.5", "below_entry_threshold"]
    assert [f.model_dump() for f in sig.risk_flags] == [
        {"code": "exchange_incident", "severity": "medium"}
    ]
    bd_ml = sig.source_breakdown["ml"]
    assert isinstance(bd_ml, dict) and bd_ml["score"] == pytest.approx(0.1556, abs=1e-4)
    bd_llm = sig.source_breakdown["llm"]
    assert isinstance(bd_llm, dict) and bd_llm["llm_call_ids"] == [99120, 99121, 99122]
    assert sig.fusion_config_version == CFG.config_version
    assert sig.expires_at == NOW + BAR // 2


def test_strong_entry_follows_ml_direction() -> None:
    long_ = fuse(ml(conf=0.95), llm(score=0.5, conf=0.8), tech(0.9), CFG, **kw())
    assert long_.action == SignalAction.BUY and long_.reason == "ok"
    short = fuse(ml("short", 0.95), llm(score=-0.5, conf=0.8), tech(-0.9), CFG, **kw())
    assert short.action == SignalAction.SELL


def test_rule1_breaker_open_holds() -> None:
    sig = fuse(ml(conf=0.95), None, tech(0.9), CFG, **kw(breaker_open=True))
    assert (sig.action, sig.reason) == (SignalAction.HOLD, "circuit_breaker_open")


@pytest.mark.parametrize(
    ("pred", "reason"),
    [
        (None, "ml_missing_or_stale"),
        (ml(ts=TS - 2 * BAR), "ml_missing_or_stale"),
        (ml("flat", 0.9), "ml_below_threshold"),
        (ml(conf=0.5), "ml_below_threshold"),
    ],
)
def test_rules2_3_ml_veto(pred: object, reason: str) -> None:
    sig = fuse(pred, llm(), tech(), CFG, **kw())  # type: ignore[arg-type]
    assert (sig.action, sig.reason) == (SignalAction.NO_SIGNAL, reason)


def test_rule4_tech_cancels_but_never_reverses() -> None:
    sig = fuse(ml(conf=0.6), None, tech(-1.0), CFG, **kw())
    assert (sig.action, sig.reason) == (SignalAction.HOLD, "tech_cancels_ml")


def test_rule6_conflict_halves_strength() -> None:
    no_llm = fuse(ml(conf=0.99), llm(status="rejected"), tech(1.0), CFG, **kw())
    conflict = fuse(ml(conf=0.99), llm(score=-1.0, conf=0.9), tech(1.0), CFG, **kw())
    assert "ml_llm_conflict" in conflict.source_breakdown["rules_fired"]  # type: ignore[operator]
    assert conflict.strength < no_llm.strength / CFG.ml_only_strength_multiplier


def test_rule7_high_flag_blocks_entry_and_reduces_open_position() -> None:
    hi = llm(score=0.9, conf=0.9, flags=[("regulatory_action", "high")])
    flat = fuse(ml(conf=0.99), hi, tech(1.0), CFG, **kw())
    assert (flat.action, flat.reason) == (SignalAction.HOLD, "llm_high_risk_flag")
    opened = fuse(ml(conf=0.99), hi, tech(1.0), CFG, **kw(position_side=1))
    assert (opened.action, opened.strength) == (SignalAction.REDUCE, 1.0)
    exit_cfg = CFG.model_copy(update={"on_high_flag_with_open_position": "exit"})
    assert fuse(ml(conf=0.99), hi, tech(1.0), exit_cfg, **kw(position_side=1)).action == (
        SignalAction.CLOSE
    )


def test_rule8_blocked_regime() -> None:
    sig = fuse(ml(conf=0.99, regime="chaotic"), llm(0.9, 0.9), tech(1.0), CFG, **kw())
    assert (sig.action, sig.reason) == (SignalAction.HOLD, "regime_chaotic")


def test_rule9_ml_only_multiplier_and_stale_llm() -> None:
    full = fuse(ml(conf=0.99), None, tech(1.0), CFG, **kw())
    assert {"llm_unavailable", "ml_only"} <= set(full.source_breakdown["rules_fired"])  # type: ignore[arg-type]
    stale = llm(0.9, 0.9, as_of=NOW - (CFG.llm_max_age_s + 1) * 1000)
    assert fuse(ml(conf=0.99), stale, tech(1.0), CFG, **kw()).strength == full.strength
    ml_score = (0.99 - CFG.ml_min_confidence) / (1 - CFG.ml_min_confidence)
    base = (CFG.weights.ml * ml_score + CFG.weights.tech * 1.0) / (
        CFG.weights.ml + CFG.weights.tech
    )
    assert full.strength == pytest.approx(base * CFG.ml_only_strength_multiplier, abs=1e-4)


def test_position_aware_actions() -> None:
    same = fuse(ml(conf=0.99), llm(0.9, 0.9), tech(1.0), CFG, **kw(position_side=1))
    opposite = fuse(ml(conf=0.99), llm(0.9, 0.9), tech(1.0), CFG, **kw(position_side=-1))
    assert same.action == SignalAction.HOLD and opposite.action == SignalAction.CLOSE


def test_fuse_is_deterministic() -> None:
    a = fuse(ml(), llm(), tech(), CFG, **kw())
    b = fuse(ml(), llm(), tech(), CFG, **kw())
    assert a == b


def test_verify_rejects_inconsistent_signal() -> None:
    p = ml(conf=0.95)
    sig = fuse(p, llm(0.5, 0.8), tech(0.9), CFG, **kw())
    assert verify(sig, p) is sig
    flipped = sig.model_copy(update={"action": SignalAction.SELL})
    with pytest.raises(FusionInvariantError, match="direction"):
        verify(flipped, p)
    with pytest.raises(FusionInvariantError, match="entry without ML"):
        verify(sig, None)
    other = p.model_copy(update={"confidence": 0.7})
    with pytest.raises(FusionInvariantError, match="differs"):
        verify(sig, other)


# --- инварианты §4.3 (property-based) --------------------------------------------------

probs = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
scores = st.floats(min_value=-1.0, max_value=1.0, allow_nan=False)
dirs = st.sampled_from(["long", "short", "flat"])
sevs = st.sampled_from(["low", "medium", "high"])
regimes = st.sampled_from(["trend_up", "trend_down", "range", "high_vol", "chaotic"])
sides = st.sampled_from([-1, 0, 1])


@settings(max_examples=300, deadline=None)
@given(d=dirs, conf=probs, ts=scores, ls=scores, lc=probs, regime=regimes, side=sides)
def test_property_entry_matches_ml_and_llm_never_amplifies(
    d: str, conf: float, ts: float, ls: float, lc: float, regime: str, side: int
) -> None:
    pred = ml(d, conf, regime)
    with_llm = fuse(pred, llm(ls, lc), tech(ts), CFG, **kw(position_side=side))
    without = fuse(pred, None, tech(ts), CFG, **kw(position_side=side))
    if with_llm.action in (SignalAction.BUY, SignalAction.SELL):
        assert with_llm.action == (SignalAction.BUY if d == "long" else SignalAction.SELL)
    # LLM не увеличивает strength относительно базы без LLM (до ML-only множителя)
    base = (
        without.strength / CFG.ml_only_strength_multiplier
        if without.reason not in ("below_entry_threshold",)
        else None
    )
    if base is not None and with_llm.action in (SignalAction.BUY, SignalAction.SELL):
        assert with_llm.strength <= base + 1e-9
    verify(with_llm, pred)


@settings(max_examples=300, deadline=None)
@given(conf=probs, ts=scores, ls=scores, lc=probs, sev=sevs, side=sides)
def test_property_risk_flag_never_increases_strength(
    conf: float, ts: float, ls: float, lc: float, sev: str, side: int
) -> None:
    pred = ml("long", conf)
    plain = fuse(pred, llm(ls, lc), tech(ts), CFG, **kw(position_side=side))
    flagged = fuse(
        pred, llm(ls, lc, [("liquidity_shock", sev)]), tech(ts), CFG, **kw(position_side=side)
    )
    entries = (SignalAction.BUY, SignalAction.SELL)
    if flagged.action in entries:
        assert plain.action in entries and flagged.strength <= plain.strength
    if sev == "high":
        assert flagged.action not in entries  # high блокирует вход


@settings(max_examples=200, deadline=None)
@given(conf=probs, ls=scores, lc=probs, regime=regimes)
def test_property_veto_never_blocks_risk_reduction(
    conf: float, ls: float, lc: float, regime: str
) -> None:
    """Открытая позиция + high-флаг → REDUCE независимо от режима и ML-порогов выше него."""
    pred = ml("long", max(conf, CFG.ml_min_confidence + 0.2), regime)
    sig = fuse(
        pred, llm(ls, lc, [("exchange_incident", "high")]), tech(1.0), CFG, **kw(position_side=1)
    )
    if sig.reason == "llm_high_risk_flag":
        assert sig.action == SignalAction.REDUCE
