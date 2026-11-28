"""`replay(decision_id)` (ML_LLM_FUSION_SPEC.md §5.5): `fuse()` на сохранённых входах.

Входы восстанавливаются из БД, без сети и повторных вызовов LLM: ML — `ml_predictions`
(по `source_breakdown.ml.prediction_id`), LLM — `llm_contexts` (по `context_id`, исходный
`response_json` — в `llm_calls`), tech и состояние (позиция, CB, now, bar) — из
`source_breakdown`. Конфиг должен иметь ту же `fusion_config_version`.
"""

import json
import sqlite3
from typing import Any

from pydantic import BaseModel, ConfigDict

from augur.config.settings import FusionConfig
from augur.fusion.contract import FusedSignal
from augur.fusion.engine import TechSignal, fuse
from augur.fusion.llm.service import load_context
from augur.fusion.ml.inference import load_prediction

__all__ = ["ReplayError", "ReplayResult", "replay_fused"]

_COMPARED = (
    "signal_id",
    "action",
    "strength",
    "reason",
    "risk_flags",
    "expires_at",
    "source_breakdown",
    "fusion_config_version",
)


class ReplayError(LookupError):
    """Решение нельзя воспроизвести: нет записи, другой конфиг или неполные входы."""


class ReplayResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fused_signal_id: str
    match: bool
    diffs: list[str]
    recorded: dict[str, Any]
    replayed: dict[str, Any]


def _fused_row(conn: sqlite3.Connection, ref_id: str) -> tuple[Any, ...]:
    cols = (
        "id, signal_id, idempotency_key, api_version, asset, timestamp, expires_at, action, "
        "strength, reason, explanation_ref, risk_flags_json, source_breakdown_json, "
        "fusion_config_version"
    )
    row = conn.execute(
        f"SELECT {cols} FROM fused_signals WHERE id=? OR signal_id=?",  # noqa: S608 — константа
        (ref_id, ref_id),
    ).fetchone()  # noqa: S608
    if row is None:
        da = conn.execute(
            "SELECT fused_signal_id FROM decision_audit WHERE id=?", (ref_id,)
        ).fetchone()
        if da is None or da[0] is None:
            raise ReplayError(f"no fused signal for {ref_id}")
        row = conn.execute(f"SELECT {cols} FROM fused_signals WHERE id=?", (da[0],)).fetchone()  # noqa: S608
    if row is None:
        raise ReplayError(f"no fused signal for {ref_id}")
    return tuple(row)


def replay_fused(conn: sqlite3.Connection, ref_id: str, cfg: FusionConfig) -> ReplayResult:
    """`ref_id` — id/signal_id `fused_signals` или id `decision_audit`."""
    r = _fused_row(conn, ref_id)
    recorded = FusedSignal(
        api_version=r[3],
        signal_id=r[1],
        idempotency_key=r[2],
        asset=r[4],
        timestamp=r[5],
        expires_at=r[6],
        action=r[7],
        strength=r[8],
        reason=r[9],
        explanation_ref=r[10],
        risk_flags=json.loads(r[11]),
        source_breakdown=json.loads(r[12]),
        fusion_config_version=r[13],
    )
    if recorded.fusion_config_version != cfg.config_version:
        raise ReplayError(
            f"fusion_config_version {recorded.fusion_config_version} "
            f"!= current {cfg.config_version}"
        )
    bd = recorded.source_breakdown
    state, ml_part, llm_part, tech_part = (bd.get(k) for k in ("state", "ml", "llm", "tech"))
    if not isinstance(state, dict) or not isinstance(tech_part, dict):
        raise ReplayError("source_breakdown has no recorded inputs (pre-0.10.0 signal?)")
    ml = None
    if isinstance(ml_part, dict) and ml_part.get("prediction_id"):
        try:
            ml = load_prediction(conn, str(ml_part["prediction_id"]))
        except KeyError as e:
            raise ReplayError(f"ml_prediction {e} not found") from e
    llm = None
    if isinstance(llm_part, dict) and llm_part.get("context_id"):
        llm = load_context(conn, str(llm_part["context_id"]))
        if llm is None:
            raise ReplayError("llm_context not found")
    tech = None
    if tech_part.get("score") is not None:
        detail = {k: v for k, v in tech_part.items() if k not in ("score", "weight", "timestamp")}
        tech = TechSignal(
            score=float(tech_part["score"]), timestamp=int(tech_part["timestamp"]), detail=detail
        )
    replayed = fuse(
        ml,
        llm,
        tech,
        cfg,
        position_side=int(state["position_side"]),
        breaker_open=bool(state["breaker_open"]),
        now_ms=int(state["now_ms"]),
        bar_ms=int(state["bar_ms"]),
        asset=recorded.asset,
        timestamp=recorded.timestamp,
        idempotency_key=recorded.idempotency_key,
        explanation_ref=recorded.explanation_ref,
    )
    rec_d = recorded.model_dump(mode="json")
    rep_d = json.loads(replayed.model_dump_json())
    diffs = [f for f in _COMPARED if rec_d[f] != rep_d[f]]
    return ReplayResult(
        fused_signal_id=str(r[0]),
        match=not diffs,
        diffs=diffs,
        recorded={f: rec_d[f] for f in _COMPARED},
        replayed={f: rep_d[f] for f in _COMPARED},
    )
