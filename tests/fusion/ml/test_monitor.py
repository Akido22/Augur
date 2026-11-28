"""Nightly job ML и PSI-дрейф → Fusion OPEN по активу (FUSION SPEC §5.4, §6.4; 0.12.0)."""

import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from augur.fusion.breaker import (
    asset_breaker_open,
    asset_breaker_status,
    close_asset_breaker,
    list_open_breakers,
    open_asset_breaker,
)
from augur.fusion.ml.features import BarArrays
from augur.fusion.ml.monitor import psi, run_monitor
from tests.fusion._ml import T0, Clock, H, insert_ohlcv, promote_to_production, small_cfg

SYM = "BTC/USDT"
VENUE = "ccxt:binance"
TRAIN_N = 1400
TAIL = 720  # = drift.window_bars по умолчанию (30 дней на 1h)


def stationary_bars(n: int, seed: int = 3) -> BarArrays:
    """Часовые бары без смены режима: эталон и окно из одного процесса."""
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.006, n)))
    o = np.concatenate(([100.0], c[:-1]))
    hi = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.002, n)))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.002, n)))
    v = rng.uniform(1, 10, n)
    return BarArrays("1h", T0 + np.arange(n, dtype=np.int64) * H, o, hi, lo, c, v)


def test_psi_same_vs_shifted() -> None:
    rng = np.random.default_rng(1)
    ref = rng.normal(0, 1, 5000)
    assert psi(ref, rng.normal(0, 1, 2000), 10) < 0.05
    assert psi(ref, rng.normal(1.0, 1, 2000), 10) > 0.25
    assert psi(np.ones(100), np.ones(50), 10) == pytest.approx(0.0)  # вырожденный эталон
    assert np.isnan(psi(np.array([]), ref, 10))


def test_breaker_open_close_is_audited(db: sqlite3.Connection) -> None:
    assert not asset_breaker_open(db, SYM)
    assert open_asset_breaker(db, SYM, trigger="ml_drift", detail={"x": 1}, actor="t", ts=10)
    assert not open_asset_breaker(db, SYM, trigger="ml_drift", detail={}, actor="t", ts=11)
    st = asset_breaker_status(db, SYM)
    assert st.state == "OPEN" and st.trigger == "ml_drift" and st.since_ts == 10
    assert [s.asset for s in list_open_breakers(db)] == [SYM]
    assert not asset_breaker_open(db, "ETH/USDT")
    assert close_asset_breaker(db, SYM, actor="cli:t", approved_by="Artem", reason="ok", ts=20)
    assert not close_asset_breaker(db, SYM, actor="cli:t", approved_by="Artem", reason="ok", ts=21)
    assert list_open_breakers(db) == []
    events = [r[0] for r in db.execute("SELECT event FROM audit_log ORDER BY id")]
    assert events == ["FUSION_BREAKER_OPENED", "FUSION_BREAKER_CLOSED"]


def test_no_model_reported(db: sqlite3.Connection) -> None:
    rep = run_monitor(
        db,
        small_cfg(),
        asset=SYM,
        timeframe="1h",
        clock=lambda: 1,
        min_confidence=0.55,
        cost_rate=0.0015,
    )
    assert rep.status == "no_model" and not rep.breaker_opened
    row = db.execute("SELECT severity, payload_json FROM audit_log WHERE event='ML_MONITOR_REPORT'")
    sev, payload = row.fetchone()
    assert sev == "WARN" and json.loads(payload)["status"] == "no_model"


# --- e2e с обученной моделью ------------------------------------------------------------


def _slice(b: BarArrays, n: int) -> BarArrays:
    return BarArrays(
        b.timeframe, b.ts_open[:n], b.open[:n], b.high[:n], b.low[:n], b.close[:n], b.volume[:n]
    )


def _drifted(b: BarArrays, start: int) -> BarArrays:
    """Хвост с другой волатильностью и диапазонами баров (сдвиг распределений признаков)."""
    c = b.close.copy()
    r = np.diff(np.log(c))
    r[start - 1 :] *= 6.0
    c = c[0] * np.exp(np.concatenate(([0.0], np.cumsum(r))))
    o = np.concatenate(([c[0]], c[:-1]))
    hi = np.maximum(o, c).copy()
    lo = np.minimum(o, c).copy()
    hi[start:] *= 1.04
    lo[start:] *= 0.96
    hi[:start], lo[:start] = b.high[:start], b.low[:start]
    o[:start], c[:start] = b.open[:start], b.close[:start]
    return BarArrays(b.timeframe, b.ts_open, o, hi, lo, c, b.volume)


@pytest.fixture
def trained(db: sqlite3.Connection, tmp_path: Path):  # type: ignore[no-untyped-def]
    pytest.importorskip("lightgbm")
    from tests.fusion._ml import train_register

    full = stationary_bars(TRAIN_N + TAIL)
    cfg = small_cfg()
    clock = Clock(full.close_time(TRAIN_N - 1))
    v = train_register(db, _slice(full, TRAIN_N), cfg, tmp_path, clock)
    promote_to_production(db, v, cfg, clock)
    return full, cfg, clock, v


def _run(db: sqlite3.Connection, cfg, clock):  # type: ignore[no-untyped-def]
    return run_monitor(
        db, cfg, asset=SYM, timeframe="1h", clock=clock, min_confidence=0.55, cost_rate=0.0015
    )


def test_no_drift_on_same_process(db: sqlite3.Connection, trained) -> None:  # type: ignore[no-untyped-def]
    full, cfg, clock, v = trained
    insert_ohlcv(db, full, SYM, VENUE)
    # прогнозы на хвосте: часть горизонтов уже закрыта → realized_return
    from augur.fusion.ml.inference import MLService
    from augur.fusion.ml.registry import ModelRegistry

    svc = MLService(db, ModelRegistry(db, clock=clock), cfg, clock=clock)
    for i in range(TRAIN_N + TAIL - 40, TRAIN_N + TAIL - 1):
        assert svc.predict(SYM, full, full.close_time(i)) is not None
    clock.now = full.close_time(TRAIN_N + TAIL - 1) + 1_000
    rep = _run(db, cfg, clock)
    assert rep.model_version == v
    assert rep.status == "ok", rep
    assert rep.psi_mean is not None and rep.psi_mean <= cfg.drift.psi_threshold
    assert "__confidence__" in rep.psi
    assert len([k for k in rep.psi if k != "__confidence__"]) == cfg.drift.top_features
    assert rep.realized_filled > 30 and rep.quality["n"] == rep.realized_filled
    assert rep.quality["hit_rate"] is not None and rep.quality["ece"] is not None
    assert not asset_breaker_open(db, SYM)
    filled = db.execute(
        "SELECT COUNT(*) FROM ml_predictions WHERE realized_return IS NOT NULL"
    ).fetchone()[0]
    assert filled == rep.realized_filled


def test_drift_opens_fusion_breaker_and_fuse_holds(db: sqlite3.Connection, trained) -> None:  # type: ignore[no-untyped-def]
    full, cfg, clock, v = trained
    bars = _drifted(full, TRAIN_N)
    insert_ohlcv(db, bars, SYM, VENUE)
    clock.now = bars.close_time(TRAIN_N + TAIL - 1) + 1_000
    rep = _run(db, cfg, clock)
    assert rep.status == "drift" and rep.breaker_opened
    assert rep.psi_mean is not None and rep.psi_mean > cfg.drift.psi_threshold
    st = asset_breaker_status(db, SYM)
    assert st.state == "OPEN" and st.trigger == "ml_drift" and st.detail["model_version"] == v
    events = [r[0] for r in db.execute("SELECT event FROM audit_log ORDER BY id")]
    assert events[-3:] == ["ML_DRIFT_DETECTED", "FUSION_BREAKER_OPENED", "ML_MONITOR_REPORT"]
    retrain = json.loads(
        db.execute("SELECT payload_json FROM audit_log WHERE event='ML_DRIFT_DETECTED'").fetchone()[
            0
        ]
    )
    assert retrain["retrain_required"] is True

    # повторный прогон не дублирует OPEN
    assert not _run(db, cfg, clock).breaker_opened

    # Fusion по активу: HOLD (правило §4.3 п. 1), другой актив не затронут
    from augur.config.settings import FusionSettings
    from augur.fusion.client import FusionRequest
    from augur.fusion.local import LocalFusionClient
    from augur.fusion.ml.inference import MLService
    from augur.fusion.ml.registry import ModelRegistry

    client = LocalFusionClient(
        db,
        FusionSettings(enabled=True, ml=cfg),
        ml=MLService(db, ModelRegistry(db, clock=clock), cfg, clock=clock),
        llm=None,
        clock=clock,
    )
    req = FusionRequest(
        asset=SYM,
        timeframe="1h",
        timestamp=bars.close_time(TRAIN_N + TAIL - 1),
        idempotency_key="k" * 64,
        context={"action": "BUY", "strength": 0.9, "strategy_signal_id": "s"},
    )
    import asyncio

    sig = asyncio.run(client.get_fused_signal(req))
    assert sig is not None and sig.action.value == "HOLD" and sig.reason == "circuit_breaker_open"

    # сброс человеком → Fusion снова считает сигнал
    close_asset_breaker(db, SYM, actor="cli:t", approved_by="Artem", reason="retrained", ts=clock())
    db.commit()
    sig2 = asyncio.run(
        client.get_fused_signal(req.model_copy(update={"idempotency_key": "z" * 64}))
    )
    assert sig2 is not None and sig2.reason != "circuit_breaker_open"


def test_insufficient_current_window(db: sqlite3.Connection, trained) -> None:  # type: ignore[no-untyped-def]
    full, cfg, clock, _ = trained
    insert_ohlcv(db, _slice(full, TRAIN_N), SYM, VENUE)  # только бары обучения
    clock.now = full.close_time(TRAIN_N - 1) + 30 * 24 * H  # месяц без новых баров
    rep = _run(db, cfg, clock)
    assert rep.status == "insufficient_data" and not asset_breaker_open(db, SYM)
