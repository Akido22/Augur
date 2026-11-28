"""Проверка выхода LLM: схема → grounding → семантика → variance filter (FUSION SPEC §5.1–5.2).

Невалидный выход отбрасывается, а не «чинится» (skill llm-output-validation).
"""

import math
import statistics
from datetime import datetime
from typing import Literal

from pydantic import ValidationError

from augur.config.settings import LLMSettings, LLMVarianceSettings
from augur.fusion.llm.schema import LLMContext, RiskFlag, RiskFlagCode, Severity

__all__ = ["CallStatus", "aggregate_samples", "parse_context"]

CallStatus = Literal["ok", "invalid_schema", "ungrounded"]


def _short(e: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(map(str, err['loc'])) or 'model'}: {err['msg']}" for err in e.errors()
    )[:500]


def parse_context(
    raw: str,
    *,
    asset: str,
    as_of: datetime,
    input_ids: set[str],
    event_ids: set[str],
    has_inputs: bool,
    cfg: LLMSettings,
) -> tuple[LLMContext | None, CallStatus, str | None]:
    try:
        ctx = LLMContext.model_validate_json(raw, context={"input_ids": input_ids})
    except ValidationError as e:
        msg = _short(e)
        return None, ("ungrounded" if "ungrounded" in msg else "invalid_schema"), msg
    if ctx.asset != asset:
        return None, "invalid_schema", f"semantic: asset {ctx.asset!r} != {asset!r}"
    if ctx.as_of != as_of:
        return None, "invalid_schema", "semantic: as_of does not match request"
    for f in ctx.risk_flags:
        if (
            f.code == RiskFlagCode.HIGH_IMPACT_EVENT_IMMINENT
            and not set(f.evidence_ids) & event_ids
        ):
            return None, "ungrounded", "semantic: high_impact_event_imminent without calendar event"
    if not has_inputs and abs(ctx.context_score) > cfg.empty_input_max_abs_score:
        return None, "invalid_schema", "semantic: non-neutral score on empty input"
    return ctx, "ok", None


_SEV_RANK = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2}


def _sign_flip(scores: list[float]) -> bool:
    return min(scores) < 0 < max(scores)


def aggregate_samples(
    samples: list[LLMContext], cfg: LLMVarianceSettings, *, requested: int | None = None
) -> tuple[LLMContext | None, str | None]:
    """Медиана скора, min(confidence), флаги большинства; high у меньшинства → medium (§5.2)."""
    asked = cfg.samples if requested is None else requested  # режим экономии: 1 сэмпл
    if not samples or len(samples) < min(cfg.min_valid, asked):
        return None, "too_few_valid_samples"
    scores = [s.context_score for s in samples]
    if len(scores) > 1 and statistics.pstdev(scores) > cfg.max_score_std:
        return None, "score_variance"
    if max(scores) - min(scores) > cfg.max_score_range and _sign_flip(scores):
        return None, "sign_disagreement"
    quorum = math.ceil(len(samples) / 2)
    by_code: dict[RiskFlagCode, list[RiskFlag]] = {}
    for s in samples:
        for f in {f.code: f for f in s.risk_flags}.values():  # один флаг кода на сэмпл
            by_code.setdefault(f.code, []).append(f)
    flags: list[RiskFlag] = []
    for code in sorted(by_code, key=lambda c: c.value):
        found = by_code[code]
        evidence = sorted({e for f in found for e in f.evidence_ids})[:10]
        if len(found) >= quorum:
            sev = max((f.severity for f in found), key=lambda s: _SEV_RANK[s])
            flags.append(RiskFlag(code=code, severity=sev, evidence_ids=evidence))
        elif any(f.severity == Severity.HIGH for f in found):
            # пропустить реальный риск дороже, чем лишний раз уменьшить позицию
            flags.append(RiskFlag(code=code, severity=Severity.MEDIUM, evidence_ids=evidence))
    ordered = sorted(range(len(samples)), key=lambda i: (scores[i], i))
    median_idx = ordered[(len(ordered) - 1) // 2]
    base = samples[median_idx]
    return (
        LLMContext.model_validate(
            {
                "schema_version": "1.0",
                "asset": base.asset,
                "as_of": base.as_of,
                "context_score": statistics.median(scores),
                "risk_flags": flags,
                "confidence": min(s.confidence for s in samples),
                "rationale": base.rationale,
            },
            context={"input_ids": {e for f in flags for e in f.evidence_ids}},
            strict=False,
        ),
        None,
    )
