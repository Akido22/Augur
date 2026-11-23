"""FusedSignal: структурная валидация контракта (ML_LLM_FUSION_SPEC.md §4.6)."""

from typing import Any

import pytest
from pydantic import ValidationError

from augur import versions
from augur.fusion import FusedSignal, RiskFlag
from augur.strategies.base import SignalAction


def _payload(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "api_version": versions.API_VERSION,
        "signal_id": "0b7f6d0e-2c41-4e7a-8f7e-91d3c6a2b001",
        "idempotency_key": "k1",
        "asset": "BTC/USDT",
        "timestamp": 1_000,
        "expires_at": 2_000,
        "action": "HOLD",
        "strength": 0.25,
        "reason": "below_entry_threshold",
        "explanation_ref": "fusion_stub",
        "risk_flags": [{"code": "exchange_incident", "severity": "medium"}],
        "source_breakdown": {"ml": 0.0, "tech": 1.0, "llm": 0.0, "rules_fired": []},
        "fusion_config_version": "stub-0.4.0",
    }
    data.update(overrides)
    return data


def test_valid_fused_signal() -> None:
    s = FusedSignal.model_validate(_payload())
    assert s.action is SignalAction.HOLD
    assert s.risk_flags == [RiskFlag(code="exchange_incident", severity="medium")]
    assert s.api_version == versions.API_VERSION


def test_api_version_mismatch() -> None:
    with pytest.raises(ValidationError, match="api_version mismatch: got 2.0, expected"):
        FusedSignal.model_validate(_payload(api_version="2.0"))


@pytest.mark.parametrize("expires_at", [1_000, 999])
def test_expires_at_must_be_after_timestamp(expires_at: int) -> None:
    with pytest.raises(ValidationError, match="expires_at"):
        FusedSignal.model_validate(_payload(expires_at=expires_at))


@pytest.mark.parametrize("strength", [-0.01, 1.01])
def test_strength_out_of_range(strength: float) -> None:
    with pytest.raises(ValidationError, match="strength"):
        FusedSignal.model_validate(_payload(strength=strength))


@pytest.mark.parametrize("action", ["enter_long", "hold", "BUY_MORE", ""])
def test_action_outside_six_values(action: str) -> None:
    with pytest.raises(ValidationError, match="action"):
        FusedSignal.model_validate(_payload(action=action))


def test_all_six_actions_accepted() -> None:
    for action in SignalAction:
        assert FusedSignal.model_validate(_payload(action=action.value)).action is action


def test_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FusedSignal.model_validate(_payload(ttl_seconds=60))


@pytest.mark.parametrize("ref", ["", "   "])
def test_empty_explanation_ref(ref: str) -> None:
    with pytest.raises(ValidationError, match="explanation_ref"):
        FusedSignal.model_validate(_payload(explanation_ref=ref))


def test_explanation_ref_required() -> None:
    data = _payload()
    del data["explanation_ref"]
    with pytest.raises(ValidationError, match="explanation_ref"):
        FusedSignal.model_validate(data)


def test_signal_id_must_be_uuid() -> None:
    with pytest.raises(ValidationError, match="signal_id"):
        FusedSignal.model_validate(_payload(signal_id="not-a-uuid"))


def test_risk_flag_severity_restricted() -> None:
    with pytest.raises(ValidationError, match="severity"):
        FusedSignal.model_validate(_payload(risk_flags=[{"code": "x", "severity": "critical"}]))


def test_fused_signal_is_frozen() -> None:
    s = FusedSignal.model_validate(_payload())
    with pytest.raises(ValidationError):
        s.strength = 1.0  # type: ignore[misc]
