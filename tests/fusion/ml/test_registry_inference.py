"""Обучение, реестр, загрузка со сверкой хэшей, инференс и replay (skill ml п. 13)."""

import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("lightgbm")

from augur.backtest.costs import CostModel  # noqa: E402
from augur.config.settings import MLPromotionSettings  # noqa: E402
from augur.fusion.ml.artifact import artifact_sha256  # noqa: E402
from augur.fusion.ml.inference import MLService, PredictionMismatch, replay_prediction  # noqa: E402
from augur.fusion.ml.registry import (  # noqa: E402
    ModelRegistry,
    ModelUnavailable,
    PromotionError,
    RegistrationError,
    model_name,
)
from augur.fusion.ml.train import train_model  # noqa: E402
from tests.fusion._ml import (  # noqa: E402
    DAY,
    Clock,
    promote_to_production,
    small_cfg,
    synth_bars,
    train_register,
)

NOW = 1_800_000_000_000


def test_training_is_deterministic(tmp_path: Path) -> None:
    bars, cfg = synth_bars(), small_cfg()
    kw = dict(
        asset="BTC/USDT",
        name="n",
        version="lightgbm_v1",
        cfg=cfg,
        min_confidence=0.55,
        costs=CostModel(),
    )
    a = train_model(bars, artifacts_dir=tmp_path / "a", **kw)  # type: ignore[arg-type]
    b = train_model(bars, artifacts_dir=tmp_path / "b", **kw)  # type: ignore[arg-type]
    assert a.sha256 == b.sha256
    assert set(a.metrics) >= {"logloss", "brier", "ece", "sharpe", "max_dd", "trials"}
    assert a.metrics["trials"] == 1 and a.metrics["folds"] >= 1
    assert sorted(p.name for p in a.path.iterdir()) == ["calibrator.json", "meta.json", "model.txt"]
    assert not list(a.path.glob("*.pkl"))


def test_register_requires_calibrator_and_next_version(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    clock = Clock(NOW)
    reg = ModelRegistry(db, clock=clock)
    res = train_model(
        synth_bars(),
        asset="BTC/USDT",
        name="x",
        version="lightgbm_v1",
        cfg=small_cfg(),
        min_confidence=0.55,
        costs=CostModel(),
        artifacts_dir=tmp_path,
    )
    with pytest.raises(RegistrationError, match="version"):
        reg.register(
            name="x", version="lightgbm_v7", artifact_path=res.path, trained_at=NOW, actor="t"
        )
    (res.path / "calibrator.json").unlink()
    with pytest.raises(RegistrationError, match="calibrator"):
        reg.register(
            name="x", version="lightgbm_v1", artifact_path=res.path, trained_at=NOW, actor="t"
        )


def test_lifecycle_promotion_rules(db: sqlite3.Connection, tmp_path: Path) -> None:
    clock, cfg = Clock(NOW), small_cfg()
    v1 = train_register(db, synth_bars(), cfg, tmp_path, clock)
    reg = ModelRegistry(db, clock=clock)
    assert reg.by_version(v1).status == "candidate" and v1 == "lightgbm_v1"
    rules = MLPromotionSettings(max_ece=1.0)
    with pytest.raises(PromotionError):
        reg.promote(v1, "production", approved_by="me", rules=rules)  # не через staging
    reg.promote(v1, "staging", approved_by="me", rules=rules)
    with pytest.raises(PromotionError, match="staging for"):
        reg.promote(v1, "production", approved_by="me", rules=rules)  # < 14 дней
    clock.now += 15 * DAY
    with pytest.raises(PromotionError, match="ECE"):
        reg.promote(v1, "production", approved_by="me", rules=MLPromotionSettings(max_ece=1e-9))
    reg.promote(v1, "production", approved_by="me", rules=rules)
    assert reg.get_production(model_name("BTC/USDT", "1h")).version == v1  # type: ignore[union-attr]
    events = [r[0] for r in db.execute("SELECT event FROM audit_log ORDER BY id")]
    assert events.count("ML_MODEL_STATUS") == 2 and "ML_MODEL_REGISTERED" in events
    # вторая production-модель на то же имя невозможна на уровне БД
    v2 = train_register(db, synth_bars(seed=8), cfg, tmp_path, clock)
    assert v2 == "lightgbm_v2"
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE ml_models SET status='production' WHERE version=?", (v2,))


def test_tampered_artifact_is_unavailable(db: sqlite3.Connection, tmp_path: Path) -> None:
    clock, cfg = Clock(NOW), small_cfg()
    v = train_register(db, synth_bars(), cfg, tmp_path, clock)
    promote_to_production(db, v, cfg, clock)
    reg = ModelRegistry(db, clock=clock)
    rec = reg.by_version(v)
    path = Path(rec.artifact_path)
    meta = json.loads((path / "meta.json").read_text("utf-8"))
    meta["barrier_k"] = 9.9
    (path / "meta.json").write_text(json.dumps(meta), "utf-8")
    assert artifact_sha256(path) != rec.artifact_sha256
    with pytest.raises(ModelUnavailable, match="sha256"):
        reg.load(rec, feature_set_version=rec.feature_set_version)
    svc = MLService(db, reg, cfg, clock=clock)
    bars = synth_bars()
    assert svc.predict("BTC/USDT", bars, bars.close_time(len(bars) - 1)) is None
    ev = db.execute("SELECT event FROM audit_log WHERE event='ML_MODEL_UNAVAILABLE'").fetchall()
    assert ev


def test_feature_set_mismatch_is_unavailable(db: sqlite3.Connection, tmp_path: Path) -> None:
    clock, cfg = Clock(NOW), small_cfg()
    v = train_register(db, synth_bars(), cfg, tmp_path, clock)
    reg = ModelRegistry(db, clock=clock)
    with pytest.raises(ModelUnavailable, match="feature_set_version"):
        reg.load(reg.by_version(v), feature_set_version="fs_other")


def test_inference_idempotent_and_replayable(db: sqlite3.Connection, tmp_path: Path) -> None:
    clock, cfg = Clock(NOW), small_cfg()
    bars = synth_bars()
    v = train_register(db, bars, cfg, tmp_path, clock)
    reg = ModelRegistry(db, clock=clock)
    svc = MLService(db, reg, cfg, clock=clock)
    as_of = bars.close_time(len(bars) - 1)
    assert svc.predict("BTC/USDT", bars, as_of) is None  # нет production-модели
    promote_to_production(db, v, cfg, clock)
    p1 = svc.predict("BTC/USDT", bars, as_of)
    p2 = MLService(db, reg, cfg, clock=clock).predict("BTC/USDT", bars, as_of)
    assert p1 is not None and p1 == p2  # тот же бар → та же строка ml_predictions
    assert db.execute("SELECT COUNT(*) FROM ml_predictions").fetchone() == (1,)
    assert p1.model_version == v and p1.input_hash.startswith("sha256:")
    assert abs(sum(p1.class_probs.values()) - 1.0) < 1e-9
    assert p1.confidence == max(p1.class_probs.values())
    assert replay_prediction(db, svc, reg, p1.prediction_id, bars) == p1
    db.execute("UPDATE ml_predictions SET input_hash='sha256:x' WHERE id=?", (p1.prediction_id,))
    with pytest.raises(PredictionMismatch, match="input_hash"):
        replay_prediction(db, svc, reg, p1.prediction_id, bars)
