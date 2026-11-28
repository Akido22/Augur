"""ML-инференс на закрытии бара (ML_LLM_FUSION_SPEC.md §2.4–2.5; skill п. 10–11).

Только production-модель из реестра; при недоступности — WARNING `ML_MODEL_UNAVAILABLE` в
`audit_log` и `None` (Fusion → «ML отсутствует → NO_SIGNAL»). Каждый прогноз пишется в
`ml_predictions` до передачи в Fusion; `UNIQUE (model_id, asset, timestamp)` — повтор того же
бара возвращает уже записанный прогноз.
"""

import json
import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from augur.config.settings import MLSettings
from augur.db.audit import append_audit
from augur.fusion.ml.artifact import Predictor
from augur.fusion.ml.features import BarArrays, compute_features, feature_set_version
from augur.fusion.ml.regime import regime_tag
from augur.fusion.ml.registry import ModelRecord, ModelRegistry, ModelUnavailable, model_name
from augur.fusion.ml.schema import CLASS_NAMES, Direction, MLPrediction
from augur.ids import new_ulid

__all__ = ["MLService", "PredictionMismatch", "load_prediction", "replay_prediction"]

log = logging.getLogger(__name__)
AUDIT_ACTOR = "ml_inference"
_ROUND = 12  # округление вероятностей при записи: устраняет шум последних битов float


class PredictionMismatch(AssertionError):
    """Повторный расчёт прогноза не совпал с записанным."""


class MLService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        registry: ModelRegistry,
        cfg: MLSettings,
        *,
        clock: Callable[[], int],
    ) -> None:
        self._conn = conn
        self._registry = registry
        self._cfg = cfg
        self._clock = clock
        self._cache: dict[str, Predictor] = {}

    def _predictor(self, rec: ModelRecord) -> Predictor:
        cached = self._cache.get(rec.artifact_sha256)
        if cached is None:
            cached = self._registry.load(
                rec, feature_set_version=feature_set_version(self._cfg.features)
            )
            self._cache = {rec.artifact_sha256: cached}
        return cached

    def compute(
        self, rec: ModelRecord, predictor: Predictor, asset: str, bars: BarArrays, as_of: int
    ) -> tuple[dict[Direction, float], Direction, float, str, str, int] | None:
        """Чистый расчёт: (class_probs, direction, confidence, regime, input_hash, ts)."""
        fv = compute_features(bars, as_of, self._cfg.features)
        if fv is None:
            return None
        probs = predictor.predict_proba([fv])[0]
        class_probs: dict[Direction, float] = {
            name: round(float(p), _ROUND) for name, p in zip(CLASS_NAMES, probs, strict=True)
        }
        direction = max(CLASS_NAMES, key=lambda c: (class_probs[c], -CLASS_NAMES.index(c)))
        return (
            class_probs,
            direction,
            class_probs[direction],
            regime_tag(fv, self._cfg.regime),
            fv.input_hash(),
            fv.as_of,
        )

    def predict(self, asset: str, bars: BarArrays, as_of: int) -> MLPrediction | None:
        name = model_name(asset, bars.timeframe)
        rec = self._registry.get_production(name)
        if rec is None:
            self._warn("no production model", name=name)
            return None
        try:
            predictor = self._predictor(rec)
        except ModelUnavailable as e:
            self._warn(str(e), name=name, version=rec.version)
            return None
        res = self.compute(rec, predictor, asset, bars, as_of)
        if res is None:
            return None
        class_probs, direction, conf, regime, input_hash, ts = res
        horizon = str(predictor.meta["horizon"])
        pid = new_ulid(self._clock())
        self._conn.execute(
            "INSERT INTO ml_predictions(id, model_id, asset, timestamp, direction, confidence, "
            "horizon, regime_tag, class_probs_json, input_hash) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(model_id, asset, timestamp) DO NOTHING",
            (
                pid,
                rec.id,
                asset,
                ts,
                direction,
                conf,
                horizon,
                regime,
                json.dumps(class_probs, sort_keys=True),
                input_hash,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM ml_predictions WHERE model_id=? AND asset=? AND timestamp=?",
            (rec.id, asset, ts),
        ).fetchone()
        stored = load_prediction(self._conn, str(row[0]))
        if stored.input_hash != input_hash:
            self._warn("stored prediction differs from recomputed", version=rec.version)
        return stored

    def _warn(self, reason: str, **payload: Any) -> None:
        log.warning("ML unavailable: %s %s", reason, payload)
        append_audit(
            self._conn,
            actor=AUDIT_ACTOR,
            event="ML_MODEL_UNAVAILABLE",
            severity="WARN",
            ref_type="ml_model",
            ref_id=str(payload.get("version") or payload.get("name") or ""),
            payload={"reason": reason, **payload},
            ts=self._clock(),
        )
        self._conn.commit()


def load_prediction(conn: sqlite3.Connection, prediction_id: str) -> MLPrediction:
    r = conn.execute(
        "SELECT p.id, p.asset, p.timestamp, p.direction, p.confidence, p.class_probs_json, "
        "p.horizon, p.regime_tag, m.version, m.feature_set_version, p.input_hash "
        "FROM ml_predictions p JOIN ml_models m ON m.id = p.model_id WHERE p.id=?",
        (prediction_id,),
    ).fetchone()
    if r is None:
        raise KeyError(prediction_id)
    return MLPrediction(
        prediction_id=r[0],
        asset=r[1],
        timestamp=r[2],
        direction=r[3],
        confidence=r[4],
        class_probs=json.loads(r[5]),
        horizon=r[6],
        regime_tag=r[7],
        model_version=r[8],
        feature_set_version=r[9],
        input_hash=r[10],
    )


def replay_prediction(
    conn: sqlite3.Connection,
    service: MLService,
    registry: ModelRegistry,
    prediction_id: str,
    bars: BarArrays,
) -> MLPrediction:
    """Пересчитывает прогноз по `model_version` и барам; расхождение → `PredictionMismatch`."""
    stored = load_prediction(conn, prediction_id)
    rec = registry.by_version(stored.model_version)
    predictor = service._predictor(rec)  # noqa: SLF001 — тот же путь загрузки с проверкой sha
    res = service.compute(rec, predictor, stored.asset, bars, stored.timestamp)
    if res is None:
        raise PredictionMismatch("not enough bars to recompute features")
    class_probs, direction, _conf, regime, input_hash, ts = res
    diffs = []
    if ts != stored.timestamp:
        diffs.append(f"timestamp {ts} != {stored.timestamp}")
    if input_hash != stored.input_hash:
        diffs.append("input_hash differs")
    if class_probs != stored.class_probs:
        diffs.append(f"class_probs {class_probs} != {stored.class_probs}")
    if direction != stored.direction or regime != stored.regime_tag:
        diffs.append("direction/regime differs")
    if diffs:
        raise PredictionMismatch("; ".join(diffs))
    return stored
