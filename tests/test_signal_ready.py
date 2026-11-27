"""SignalReady, combine_classic, from_fused (V7), SignalEngine.resolve_signal (ARCH §2.7, §5.6)."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from augur.fusion import FusedSignal, FusionRequest, StubFusionClient
from augur.fusion.contract import RiskFlag
from augur.ids import is_ulid, new_ulid
from augur.signals import SignalEngine, SignalReady, SignalRepository, StrategySignal
from augur.signals.ready import (
    FLAG_FUSION_CONTRARIAN,
    FLAG_ML_DISABLED,
    combine_classic,
    decision_id_from_ref,
)
from augur.strategies.base import SignalAction
from tests._bars import T0, H

NOW_MS = T0 + H + 1_000  # чуть позже закрытия первого бара


def _ss(
    action: SignalAction, strength: float = 0.5, strategy: str = "sma_cross", ts: int = T0
) -> StrategySignal:
    return StrategySignal(
        signal_id=new_ulid(),
        strategy_name=strategy,
        strategy_version=f"{strategy}@1.0.0",
        params_hash="p" * 64,
        data_hash="d" * 64,
        symbol="BTC/USDT",
        timeframe="1h",
        action=action,
        strength=strength,
        reason="test",
        timestamp=ts,
    )


# --- combine_classic ----------------------------------------------------------------


def test_combine_empty_is_none() -> None:
    assert combine_classic([], now_ms=NOW_MS) is None


def test_combine_single_buy_ttl_half_bar() -> None:
    s = _ss(SignalAction.BUY, 0.7)
    r = combine_classic([s], now_ms=NOW_MS)
    assert r is not None
    assert r.action == SignalAction.BUY and r.strength == 0.7 and r.source == "classic"
    assert r.strategy_signal_ids == [s.signal_id] and r.risk_flags == []
    assert (r.expires_at - r.created_at) == timedelta(milliseconds=H // 2)
    assert r.created_at.tzinfo is UTC and is_ulid(r.signal_id)


def test_combine_buy_sell_conflict_is_hold_zero() -> None:
    r = combine_classic(
        [_ss(SignalAction.BUY, 0.9), _ss(SignalAction.SELL, 0.2, "rsi")], now_ms=NOW_MS
    )
    assert r is not None and r.action == SignalAction.HOLD and r.strength == 0.0


def test_combine_majority_and_max_strength() -> None:
    r = combine_classic(
        [_ss(SignalAction.BUY, 0.3), _ss(SignalAction.BUY, 0.6, "rsi"), _ss(SignalAction.HOLD)],
        now_ms=NOW_MS,
    )
    assert r is not None and r.action == SignalAction.BUY and r.strength == 0.6


def test_combine_tie_prefers_less_risky() -> None:
    r = combine_classic([_ss(SignalAction.BUY), _ss(SignalAction.CLOSE, 0.4, "rsi")], now_ms=NOW_MS)
    assert r is not None and r.action == SignalAction.CLOSE


def test_combine_rejects_mixed_bars() -> None:
    with pytest.raises(ValueError, match="share"):
        combine_classic([_ss(SignalAction.BUY), _ss(SignalAction.BUY, ts=T0 + H)], now_ms=NOW_MS)


# --- инварианты SignalReady ---------------------------------------------------------


def _ready(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "signal_id": new_ulid(),
        "symbol": "BTC/USDT",
        "action": SignalAction.BUY,
        "strength": 0.5,
        "source": "classic",
        "explanation_ref": None,
        "risk_flags": [],
        "strategy_signal_ids": [],
        "fused_signal_id": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "expires_at": datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
    }
    base.update(over)
    return base


@pytest.mark.parametrize(
    "over",
    [
        {"source": "fusion"},  # нет fused_signal_id / explanation_ref
        {"fused_signal_id": "x"},  # classic с fused id
        {"expires_at": datetime(2026, 1, 1, tzinfo=UTC)},  # expires == created
        {"created_at": datetime(2026, 1, 1)},  # naive
        {"strength": 1.5},
        {"risk_flags": ["ml_disabled"]},  # строка вместо {code, severity}
        {"extra": 1},
    ],
)
def test_signal_ready_invariants(over: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SignalReady(**_ready(**over))  # type: ignore[arg-type]


def test_add_flag_and_with_source_return_copies() -> None:
    r = SignalReady(**_ready())  # type: ignore[arg-type]
    fb = r.with_source("classic_fallback").add_flag(FLAG_ML_DISABLED)
    assert r.source == "classic" and r.risk_flags == []
    assert fb.source == "classic_fallback" and fb.risk_flags == [FLAG_ML_DISABLED]
    assert fb.add_flag(FLAG_ML_DISABLED).risk_flags == [FLAG_ML_DISABLED]


def test_decision_id_from_ref() -> None:
    u = new_ulid()
    assert decision_id_from_ref(f"decision_audit:{u}") == u
    assert decision_id_from_ref("fusion_stub") is None
    assert decision_id_from_ref("decision_audit:not-a-ulid") is None
    assert decision_id_from_ref(None) is None


# --- from_fused / V7 ------------------------------------------------------------------


def _fused(action: SignalAction, flags: list[RiskFlag] | None = None) -> FusedSignal:
    return FusedSignal(
        api_version="1.0",
        signal_id="0b7f6d0e-2c41-4e7a-8f7e-91d3c6a2b001",
        idempotency_key="k",
        asset="BTC/USDT",
        timestamp=T0 + H,
        expires_at=T0 + H + H // 2,
        action=action,
        strength=0.6,
        reason="ok",
        explanation_ref=f"decision_audit:{new_ulid()}",
        risk_flags=flags or [],
        source_breakdown={"ml": 0.5, "tech": 1.0, "llm": 0.0, "rules_fired": []},
        fusion_config_version="test-1",
    )


def test_from_fused_contrarian_flag_when_no_agreeing_classic() -> None:
    r = SignalReady.from_fused(
        _fused(SignalAction.BUY), fused_id="F1", parents=[_ss(SignalAction.SELL)], now_ms=NOW_MS
    )
    assert r.source == "fusion" and r.fused_signal_id == "F1"
    assert FLAG_FUSION_CONTRARIAN in r.risk_flags


def test_from_fused_no_contrarian_when_classic_agrees_or_not_entry() -> None:
    agree = SignalReady.from_fused(
        _fused(SignalAction.BUY), fused_id="F", parents=[_ss(SignalAction.BUY)], now_ms=NOW_MS
    )
    hold = SignalReady.from_fused(
        _fused(SignalAction.HOLD), fused_id="F", parents=[], now_ms=NOW_MS
    )
    assert agree.risk_flags == [] and hold.risk_flags == []


def test_from_fused_keeps_fusion_flags_and_ttl() -> None:
    flag = RiskFlag(code="exchange_incident", severity="medium")
    f = _fused(SignalAction.BUY, [flag])
    r = SignalReady.from_fused(f, fused_id="F", parents=[_ss(SignalAction.BUY)], now_ms=NOW_MS)
    assert r.risk_flags == [flag]
    assert int(r.expires_at.timestamp() * 1000) == f.expires_at


# --- resolve_signal -------------------------------------------------------------------


def _repo(db: sqlite3.Connection) -> SignalRepository:
    return SignalRepository(db, config_hash="c" * 64, code_version="0.5.0")


async def test_resolve_fusion_disabled_is_classic_and_persisted(db: sqlite3.Connection) -> None:
    s = _ss(SignalAction.BUY, 0.7)
    eng = SignalEngine(StubFusionClient(), clock=lambda: NOW_MS, repo=_repo(db))
    r = await eng.resolve_signal([s])
    assert r is not None and r.source == "classic" and r.action == SignalAction.BUY
    rows = db.execute("SELECT id, kind, strategy, status FROM signals ORDER BY kind").fetchall()
    assert rows == [
        (r.signal_id, "final", None, "NEW"),
        (s.signal_id, "strategy", "sma_cross@1.0.0", "NEW"),
    ]
    assert await eng.resolve_signal([]) is None


async def test_resolve_with_stub_returns_fusion_hold_with_decision_ref(
    db: sqlite3.Connection,
) -> None:
    eng = SignalEngine(
        StubFusionClient(), clock=lambda: NOW_MS, fusion_enabled=True, repo=_repo(db)
    )
    r = await eng.resolve_signal([_ss(SignalAction.BUY)])
    assert r is not None and r.source == "fusion" and r.action == SignalAction.HOLD
    assert decision_id_from_ref(r.explanation_ref) is not None
    fused = db.execute("SELECT id, valid, explanation_ref FROM fused_signals").fetchall()
    assert fused == [(r.fused_signal_id, 1, r.explanation_ref)]
    final = db.execute("SELECT fused_signal_id FROM signals WHERE kind='final'").fetchone()
    assert final == (r.fused_signal_id,)


class _Boom:
    async def get_fused_signal(self, request: FusionRequest) -> FusedSignal | None:
        raise RuntimeError("down")


class _Slow:
    async def get_fused_signal(self, request: FusionRequest) -> FusedSignal | None:
        await asyncio.sleep(1)
        return None


class _Expired:
    async def get_fused_signal(self, request: FusionRequest) -> FusedSignal | None:
        f = await StubFusionClient().get_fused_signal(request)
        assert f is not None
        return f.model_copy(update={"expires_at": NOW_MS - 1, "timestamp": NOW_MS - 2})


@pytest.mark.parametrize(
    ("client", "event"),
    [(_Boom(), "FUSION_FALLBACK"), (_Slow(), "FUSION_FALLBACK"), (_Expired(), "FUSION_INVALID")],
)
async def test_resolve_fallback_flags_ml_disabled(
    db: sqlite3.Connection, client: object, event: str
) -> None:
    eng = SignalEngine(
        client,  # type: ignore[arg-type]
        clock=lambda: NOW_MS,
        fusion_enabled=True,
        fusion_timeout_ms=50,
        repo=_repo(db),
    )
    r = await eng.resolve_signal([_ss(SignalAction.BUY, 0.7)])
    assert r is not None and r.source == "classic_fallback" and r.action == SignalAction.BUY
    assert r.risk_flags == [FLAG_ML_DISABLED] and r.fused_signal_id is None
    ev = db.execute("SELECT event, payload_json FROM audit_log").fetchall()
    assert [e for e, _ in ev] == [event]
    assert json.loads(ev[0][1])["fallback"] == "ml_disabled"
    if event == "FUSION_INVALID":  # невалидный выход сохранён с valid=0 (для streak CB)
        assert db.execute("SELECT valid FROM fused_signals").fetchall() == [(0,)]
