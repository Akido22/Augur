"""Фабрики входов fuse(): MLPrediction, LLMContextResult, TechSignal."""

from datetime import UTC, datetime
from typing import Any

from augur.fusion.engine import TechSignal
from augur.fusion.llm.schema import LLMContext, LLMContextResult, RiskFlag
from augur.fusion.ml.schema import MLPrediction

BAR = 3_600_000
TS = 1_795_161_600_000  # close_time бара
NOW = TS + 1_000
IDEM = "k" * 64
REF = "decision_audit:01JD3XB4K7M2N8P9Q0R1S2T3V4"


def ml(
    direction: str = "long", conf: float = 0.62, regime: str = "trend_up", ts: int = TS
) -> MLPrediction:
    rest = (1 - conf) / 2
    probs = {"long": rest, "flat": rest, "short": rest}
    probs[direction] = conf  # type: ignore[index]
    return MLPrediction(
        prediction_id="01JD3XB4K7M2N8P9Q0R1S2T3V5",
        asset="BTC/USDT",
        timestamp=ts,
        direction=direction,
        confidence=conf,
        class_probs=probs,  # type: ignore[arg-type]
        horizon="4h",
        regime_tag=regime,
        model_version="lightgbm_v3",  # type: ignore[arg-type]
        feature_set_version="fs_9c41e07d2b1a",
        input_hash="sha256:x",
    )


def llm(
    score: float = -0.10,
    conf: float = 0.6,
    flags: list[tuple[str, str]] | None = None,
    status: str = "ok",
    as_of: int = TS,
) -> LLMContextResult:
    fl = [
        RiskFlag.model_validate(
            {"code": c, "severity": s, "evidence_ids": ["news_1"]}, strict=False
        )
        for c, s in (flags or [])
    ]
    ctx = LLMContext.model_validate(
        {
            "schema_version": "1.0",
            "asset": "BTC/USDT",
            "as_of": datetime.fromtimestamp(as_of / 1000, tz=UTC),
            "context_score": score,
            "risk_flags": fl,
            "confidence": conf,
            "rationale": "r",
        },
        context={"input_ids": {"news_1"}},
        strict=False,
    )
    return LLMContextResult(
        status=status,
        context=ctx if status == "ok" else None,  # type: ignore[arg-type]
        llm_call_ids=[99120, 99121, 99122],
        reject_reason=None,
        prompt_version="prompt_context_v1",
        context_id="01JD3XB4K7M2N8P9Q0R1S2T3V6",
    )


def tech(score: float = 0.40, ts: int = TS) -> TechSignal:
    return TechSignal(score=score, timestamp=ts, detail={"action": "BUY"})


def kw(**over: Any) -> dict[str, Any]:
    base = dict(
        position_side=0,
        breaker_open=False,
        now_ms=NOW,
        bar_ms=BAR,
        asset="BTC/USDT",
        timestamp=TS,
        idempotency_key=IDEM,
        explanation_ref=REF,
    )
    base.update(over)
    return base
