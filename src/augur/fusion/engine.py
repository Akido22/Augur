"""`fuse()` и проверка инвариантов (ML_LLM_FUSION_SPEC.md §4.3–4.6, §5; skill hybrid-ml-llm-fusion).

`fuse()` — чистая детерминированная функция: без I/O, часов и случайности. Числа — только из
`FusionConfig`. Внутреннее действие (§4.5 `Action`) маппится в `SignalAction` (ARCH §2.7).
Направление входа задаёт только ML; tech может погасить, LLM — только ослабить или заблокировать.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from augur import versions
from augur.config.settings import FusionConfig
from augur.fusion.contract import FusedSignal, RiskFlag
from augur.fusion.llm.schema import LLMContextResult, Severity
from augur.fusion.ml.schema import MLPrediction
from augur.strategies.base import SignalAction

__all__ = [
    "FUSION_NAMESPACE",
    "Action",
    "FusionInvariantError",
    "TechSignal",
    "fuse",
    "verify",
]

FUSION_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "augur:fusion:engine")


class Action(StrEnum):
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"
    REDUCE = "reduce"
    HOLD = "hold"
    NO_SIGNAL = "no_signal"


TO_SIGNAL_ACTION = {
    Action.ENTER_LONG: SignalAction.BUY,
    Action.ENTER_SHORT: SignalAction.SELL,
    Action.EXIT: SignalAction.CLOSE,
    Action.REDUCE: SignalAction.REDUCE,
    Action.HOLD: SignalAction.HOLD,
    Action.NO_SIGNAL: SignalAction.NO_SIGNAL,
}


@dataclass(frozen=True)
class TechSignal:
    """Технический сигнал стратегий, `score ∈ [-1, 1]`; `timestamp` — close_time бара."""

    score: float
    timestamp: int
    detail: dict[str, Any]


class FusionInvariantError(ValueError):
    """Нарушен инвариант §5 — сигнал невалиден (Signal Engine → ML-disabled)."""


def _wavg(parts: list[tuple[float, float | None]]) -> float:
    live = [(w, s) for w, s in parts if s is not None and w > 0]
    den = sum(w for w, _ in live)
    return sum(w * s for w, s in live) / den if den else 0.0


def fuse(
    ml: MLPrediction | None,
    llm: LLMContextResult | None,
    tech: TechSignal | None,
    cfg: FusionConfig,
    *,
    position_side: int,
    breaker_open: bool,
    now_ms: int,
    bar_ms: int,
    asset: str,
    timestamp: int,
    idempotency_key: str,
    explanation_ref: str,
) -> FusedSignal:
    """Правила §4.3 строго по порядку; `position_side` ∈ {-1, 0, 1}."""
    rules: list[str] = []
    flags: list[RiskFlag] = []
    bd: dict[str, Any] = {
        "state": {
            "position_side": position_side,
            "breaker_open": breaker_open,
            "now_ms": now_ms,
            "bar_ms": bar_ms,
        },
        "tech": (
            {
                "score": tech.score,
                "weight": cfg.weights.tech,
                "timestamp": tech.timestamp,
                **tech.detail,
            }
            if tech
            else {"score": None, "weight": cfg.weights.tech}
        ),
        "ml": {"status": "missing", "weight": cfg.weights.ml},
        "llm": _llm_part(llm, cfg),
    }

    def out(act: Action, strength: float, reason: str) -> FusedSignal:
        if reason not in rules:
            rules.append(reason)
        bd["rules_fired"] = list(rules)
        return FusedSignal(
            api_version=versions.API_VERSION,
            signal_id=str(uuid.uuid5(FUSION_NAMESPACE, f"{idempotency_key}|{cfg.config_version}")),
            idempotency_key=idempotency_key,
            asset=asset,
            timestamp=timestamp,
            expires_at=max(now_ms, timestamp)
            + max(1, int(bar_ms * cfg.signal_ttl_fraction_of_bar)),
            action=TO_SIGNAL_ACTION[act],
            strength=strength,
            reason=reason,
            explanation_ref=explanation_ref,
            risk_flags=list(flags),
            source_breakdown=bd,
            fusion_config_version=cfg.config_version,
        )

    # 1. Circuit breaker
    if breaker_open:
        return out(Action.HOLD, 0.0, "circuit_breaker_open")

    # 2–3. ML: наличие, свежесть, veto по confidence
    if ml is None or now_ms - ml.timestamp > bar_ms:
        return out(Action.NO_SIGNAL, 0.0, "ml_missing_or_stale")
    d = 1 if ml.direction == "long" else -1
    ml_score = (
        d * (ml.confidence - cfg.ml_min_confidence) / (1 - cfg.ml_min_confidence)
        if ml.direction != "flat"
        else 0.0
    )
    bd["ml"] = {
        "direction": ml.direction,
        "confidence": ml.confidence,
        "score": round(ml_score, 6),
        "weight": cfg.weights.ml,
        "model_version": ml.model_version,
        "prediction_id": ml.prediction_id,
        "regime_tag": ml.regime_tag,
        "timestamp": ml.timestamp,
    }
    if ml.direction == "flat" or ml.confidence < cfg.ml_min_confidence:
        return out(Action.NO_SIGNAL, 0.0, "ml_below_threshold")

    tech_score = tech.score if tech is not None and tech.timestamp == ml.timestamp else None
    if tech is not None and tech_score is None:
        rules.append("tech_stale")

    ctx = llm.context if llm is not None and llm.status == "ok" else None
    if ctx is not None and now_ms - int(ctx.as_of.timestamp() * 1000) > cfg.llm_max_age_s * 1000:
        ctx = None
    llm_score = ctx.context_score * ctx.confidence if ctx is not None else None
    if ctx is None:
        rules.append("llm_unavailable")

    # 4. Направление задаёт ML; tech может только погасить
    base = _wavg([(cfg.weights.ml, ml_score), (cfg.weights.tech, tech_score)])
    if d * base <= 0:
        return out(Action.HOLD, 0.0, "tech_cancels_ml")

    strength = abs(base)
    if ctx is not None and llm_score is not None:
        # 5. LLM только ослабляет
        full = _wavg(
            [
                (cfg.weights.ml, ml_score),
                (cfg.weights.tech, tech_score),
                (cfg.weights.llm, llm_score),
            ]
        )
        with_llm = max(0.0, d * full)
        if cfg.llm_can_amplify:
            strength = with_llm
        elif with_llm < strength:
            strength = with_llm
            rules.append("llm_min_dampening")
        # 6. Конфликт ML и LLM → приоритет меньшего риска
        if d * llm_score <= -cfg.conflict.llm_opposite_threshold:
            strength *= cfg.conflict.strength_multiplier
            rules.append("ml_llm_conflict")
        # 7. Veto по risk_flags
        flags.extend(RiskFlag(code=f.code.value, severity=f.severity.value) for f in ctx.risk_flags)
        sev = {f.severity for f in ctx.risk_flags}
        if Severity.HIGH in sev:
            if position_side != 0:
                act = Action(cfg.on_high_flag_with_open_position)
                return out(act, 1.0 if act != Action.HOLD else 0.0, "llm_high_risk_flag")
            return out(Action.HOLD, 0.0, "llm_high_risk_flag")
        if Severity.MEDIUM in sev:
            strength *= cfg.medium_flag_multiplier
            rules.append(f"medium_flag_x{cfg.medium_flag_multiplier:g}")
    else:
        # 9. ML-only: без LLM-фильтра риска — меньше размер
        strength *= cfg.ml_only_strength_multiplier
        rules.append("ml_only")

    # 8. Режим рынка
    if ml.regime_tag in cfg.blocked_entry_regimes and position_side == 0:
        return out(Action.HOLD, 0.0, f"regime_{ml.regime_tag}")

    # 10. Порог
    strength = round(min(max(strength, 0.0), 1.0), 4)
    if strength < cfg.entry_threshold:
        return out(Action.HOLD, strength, "below_entry_threshold")
    if strength >= cfg.strong_threshold:
        rules.append("strong")

    # Действие с учётом позиции: без пирамидинга и разворота в один шаг
    if position_side == 0:
        act = Action.ENTER_LONG if d > 0 else Action.ENTER_SHORT
    elif position_side == d:
        act = Action.HOLD  # добор — только решением Risk Engine
    else:
        act = Action.EXIT  # разворот = EXIT, затем новый сигнал
    return out(act, strength, "ok")


def _llm_part(llm: LLMContextResult | None, cfg: FusionConfig) -> dict[str, Any]:
    if llm is None:
        return {"status": "missing", "weight": cfg.weights.llm, "llm_call_ids": []}
    part: dict[str, Any] = {
        "status": llm.status,
        "weight": cfg.weights.llm,
        "llm_call_ids": list(llm.llm_call_ids),
        "prompt_version": llm.prompt_version,
        "context_id": llm.context_id,
        "reject_reason": llm.reject_reason,
    }
    if llm.context is not None:
        c = llm.context
        part.update(
            {
                "context_score": c.context_score,
                "confidence": c.confidence,
                "score": round(c.context_score * c.confidence, 6),
                "as_of": int(c.as_of.timestamp() * 1000),
                "risk_flags": [
                    {"code": f.code.value, "severity": f.severity.value} for f in c.risk_flags
                ],
            }
        )
    return part


def verify(sig: FusedSignal, ml: MLPrediction | None) -> FusedSignal:
    """Constraint-based verification (§5, skill п. 7). Нарушение → `FusionInvariantError`."""
    errors: list[str] = []
    if sig.api_version != versions.API_VERSION:
        errors.append("api_version")
    if not 0.0 <= sig.strength <= 1.0:
        errors.append("strength out of [0, 1]")
    if sig.expires_at <= sig.timestamp:
        errors.append("expires_at <= timestamp")
    if not sig.explanation_ref.strip():
        errors.append("empty explanation_ref")
    ml_part = sig.source_breakdown.get("ml")
    if sig.action in (SignalAction.BUY, SignalAction.SELL):
        if ml is None:
            errors.append("entry without ML prediction")
        else:
            expected = SignalAction.BUY if ml.direction == "long" else SignalAction.SELL
            if ml.direction == "flat" or sig.action != expected:
                errors.append("entry direction differs from ML direction")
        if any(f.severity == "high" for f in sig.risk_flags):
            errors.append("entry with high risk flag")
    if ml is not None:
        if not isinstance(ml_part, dict) or ml_part.get("prediction_id") != ml.prediction_id:
            errors.append("source_breakdown.ml does not reference the ML prediction")
        elif ml_part.get("confidence") != ml.confidence or (
            ml_part.get("model_version") != ml.model_version
        ):
            errors.append("source_breakdown.ml differs from ml_predictions")
    if errors:
        raise FusionInvariantError("; ".join(errors))
    return sig
