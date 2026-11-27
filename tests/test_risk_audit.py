"""decision_audit: запись каждого вердикта, версии, атомарность, e2e Signal Engine → Risk Gate."""

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest

from augur.config.settings import RiskLimits, TradingMode
from augur.fusion import StubFusionClient
from augur.risk import (
    DecisionAuditWriter,
    MarketSnapshot,
    RiskContextBuilder,
    RiskGate,
    UnauditableOrderError,
)
from augur.risk.audit import DecisionAuditRecord, assert_auditable
from augur.signals import SignalEngine, SignalReady, SignalRepository
from augur.signals.ready import decision_id_from_ref, ms_to_dt
from augur.strategies.base import SignalAction
from tests._risk import INSTRUMENT, make_ctx
from tests.test_signal_ready import NOW_MS, _ss

CFG = "c" * 64


def _writer(db: sqlite3.Connection, app_version: str = "0.5.0") -> DecisionAuditWriter:
    return DecisionAuditWriter(db, app_version=app_version, config_hash=CFG)


async def _classic(db: sqlite3.Connection, action: SignalAction = SignalAction.BUY) -> SignalReady:
    repo = SignalRepository(db, config_hash=CFG, code_version="0.5.0")
    eng = SignalEngine(StubFusionClient(), clock=lambda: NOW_MS, repo=repo)
    sig = await eng.resolve_signal([_ss(action, 0.7)])
    assert sig is not None
    return sig


def _ctx_now(**over: object) -> object:
    return make_ctx(now=ms_to_dt(NOW_MS), **over)


async def test_approved_verdict_written(db: sqlite3.Connection) -> None:
    sig = await _classic(db)
    v = RiskGate(RiskLimits()).evaluate_sync(sig, _ctx_now())  # type: ignore[arg-type]
    assert v.approved
    rec = _writer(db).record(sig, v)
    row = db.execute(
        "SELECT signal_id, fused_signal_id, app_version, strategy_version, ml_model_version, "
        "llm_prompt_version, llm_status, risk_verdict, risk_reason, config_hash, mode, order_id "
        "FROM decision_audit WHERE id = ?",
        (rec.id,),
    ).fetchone()
    assert row == (
        sig.signal_id, None, "0.5.0", "sma_cross@1.0.0", "n/a", "n/a", None,
        "APPROVED", None, CFG, "PAPER", None,
    )  # fmt: skip
    assert db.execute("SELECT status FROM signals WHERE id=?", (sig.signal_id,)).fetchone() == (
        "APPROVED",
    )
    event, payload = db.execute("SELECT event, payload_json FROM audit_log").fetchone()
    assert event == "RISK_APPROVED"
    data = json.loads(payload)
    assert data["decision_audit_id"] == rec.id and len(data["verdict"]["checks"]) == 18


async def test_rejected_verdict_written_with_reason(db: sqlite3.Connection) -> None:
    sig = await _classic(db)
    v = RiskGate(RiskLimits()).evaluate_sync(sig, _ctx_now(spread_bps=Decimal("41.2")))  # type: ignore[arg-type]
    rec = _writer(db).record(sig, v)
    assert rec.risk_verdict == "REJECTED" and rec.risk_reason == "spread: spread_bps above limit"
    assert db.execute("SELECT status FROM signals WHERE id=?", (sig.signal_id,)).fetchone() == (
        "REJECTED",
    )
    event, payload = db.execute("SELECT event, payload_json FROM audit_log").fetchone()
    assert event == "RISK_REJECTED"
    failed = [c["name"] for c in json.loads(payload)["verdict"]["checks"] if not c["passed"]]
    assert failed == ["spread"]


async def test_hold_is_audited_as_rejected(db: sqlite3.Connection) -> None:
    """FUSION SPEC §5.5: decision_audit пишется и для HOLD/NO_SIGNAL."""
    repo = SignalRepository(db, config_hash=CFG, code_version="0.5.0")
    eng = SignalEngine(StubFusionClient(), clock=lambda: NOW_MS, fusion_enabled=True, repo=repo)
    sig = await eng.resolve_signal([_ss(SignalAction.BUY)])
    assert sig is not None and sig.action == SignalAction.HOLD
    v = RiskGate(RiskLimits()).evaluate_sync(sig, _ctx_now())  # type: ignore[arg-type]
    rec = _writer(db).record(sig, v)
    # id записи = ULID, заранее выданный Signal Engine в explanation_ref (FUSION SPEC §4.6)
    assert rec.id == decision_id_from_ref(sig.explanation_ref)
    assert rec.fused_signal_id == sig.fused_signal_id and rec.risk_verdict == "REJECTED"
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


async def test_unknown_app_version_refused_nothing_written(db: sqlite3.Connection) -> None:
    sig = await _classic(db)
    v = RiskGate(RiskLimits()).evaluate_sync(sig, _ctx_now())  # type: ignore[arg-type]
    with pytest.raises(UnauditableOrderError, match="app_version"):
        _writer(db, "0.0.0+unknown").record(sig, v)
    assert db.execute("SELECT COUNT(*) FROM decision_audit").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM audit_log").fetchone() == (0,)


@pytest.mark.parametrize("field", ["strategy_version", "ml_model_version", "llm_prompt_version"])
@pytest.mark.parametrize("value", ["", "latest", "DEV", " unknown "])
def test_assert_auditable_forbidden_values(field: str, value: str) -> None:
    base = {
        "id": "X", "signal_id": "S", "fused_signal_id": None, "app_version": "0.5.0",
        "strategy_version": "sma_cross@1.0.0", "ml_model_version": "n/a",
        "llm_prompt_version": "n/a", "ml_prediction_id": None, "llm_status": None,
        "risk_verdict": "APPROVED", "risk_reason": None, "config_hash": CFG, "mode": "PAPER",
        "created_at": 1,
    }  # fmt: skip
    base[field] = value
    with pytest.raises(UnauditableOrderError, match=field):
        assert_auditable(DecisionAuditRecord(**base))  # type: ignore[arg-type]


async def test_missing_strategy_signals_refused(db: sqlite3.Connection) -> None:
    sig = await _classic(db)
    orphan = sig.model_copy(update={"strategy_signal_ids": ["01ZZZZZZZZZZZZZZZZZZZZZZZZ"]})
    v = RiskGate(RiskLimits()).evaluate_sync(orphan, _ctx_now())  # type: ignore[arg-type]
    with pytest.raises(UnauditableOrderError, match="strategy_version"):
        _writer(db).record(orphan, v)


async def test_db_error_rolls_back_whole_transaction(db: sqlite3.Connection) -> None:
    sig = await _classic(db)
    v = RiskGate(RiskLimits()).evaluate_sync(sig, _ctx_now())  # type: ignore[arg-type]
    db.execute(
        "CREATE TEMP TRIGGER fail_audit BEFORE INSERT ON audit_log "
        "BEGIN SELECT RAISE(ABORT, 'audit down'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="audit down"):
        _writer(db).record(sig, v)
    assert db.execute("SELECT COUNT(*) FROM decision_audit").fetchone() == (0,)
    assert db.execute("SELECT status FROM signals WHERE id=?", (sig.signal_id,)).fetchone() == (
        "NEW",
    )


async def test_decision_audit_is_append_only(db: sqlite3.Connection) -> None:
    sig = await _classic(db)
    v = RiskGate(RiskLimits()).evaluate_sync(sig, _ctx_now())  # type: ignore[arg-type]
    rec = _writer(db).record(sig, v)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE decision_audit SET risk_verdict='REJECTED' WHERE id=?", (rec.id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM decision_audit WHERE id=?", (rec.id,))


async def test_e2e_context_from_db(db: sqlite3.Connection) -> None:
    """Сигнал → контекст из БД (позиции, снимок PnL, ордера) → вердикт → аудит."""
    sig = await _classic(db)
    db.execute(
        "INSERT INTO portfolio_snapshots(ts, mode, equity, cash, day_pnl, peak_equity, "
        "drawdown_pct, positions_json) VALUES (1, 'PAPER', '9700', '9700', '-300', '10000', "
        "'0.03', '{}')"
    )
    builder = RiskContextBuilder(db, configured_mode=TradingMode.PAPER)
    market = MarketSnapshot(
        market_status=make_ctx().market_status,
        price=Decimal("100"),
        spread_bps=Decimal("5"),
        volatility_zscore=Decimal("0.5"),
        liquidity_quote=Decimal("1000000"),
        instrument=INSTRUMENT,
        equity=Decimal("9700"),
        cash=Decimal("9700"),
    )
    ctx = builder.build(sig, market, now=ms_to_dt(NOW_MS) + timedelta(seconds=1))
    assert ctx.day_pnl == Decimal("-300") and ctx.equity_start_of_day == Decimal("10000")
    assert ctx.signal_strategies == frozenset({"sma_cross@1.0.0"})
    v = RiskGate(RiskLimits()).evaluate_sync(sig, ctx)
    assert [c.name for c in v.failed] == ["daily_loss"]  # -300 ≤ -3% × 10000
    rec = _writer(db).record(sig, v)
    assert rec.risk_verdict == "REJECTED"
