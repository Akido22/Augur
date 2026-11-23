"""StubFusionClient: неисполняемые HOLD/NO_SIGNAL, детерминированность по idempotency_key."""

from augur import versions
from augur.fusion import FusedSignal, FusionRequest, StubFusionClient
from augur.strategies.base import SignalAction


def _request(key: str = "key-1", **context: object) -> FusionRequest:
    return FusionRequest(
        asset="BTC/USDT",
        timeframe="1h",
        timestamp=1_704_070_800_000,
        idempotency_key=key,
        context=dict(context),
    )


async def test_returns_hold_or_no_signal() -> None:
    client = StubFusionClient()
    with_tech = await client.get_fused_signal(
        _request(strategy_signal_id="01J0000000000000000000000")
    )
    without = await client.get_fused_signal(_request())
    assert with_tech is not None and with_tech.action is SignalAction.HOLD
    assert without is not None and without.action is SignalAction.NO_SIGNAL
    for s in (with_tech, without):
        assert s.strength == 0.0
        assert s.reason == "fusion_stub"
        assert s.risk_flags == []
        assert s.source_breakdown == {"ml": 0.0, "tech": 1.0, "llm": 0.0, "rules_fired": []}
        assert s.fusion_config_version == "stub-0.4.0"


async def test_deterministic_by_idempotency_key() -> None:
    client = StubFusionClient()
    a = await client.get_fused_signal(_request("same"))
    b = await client.get_fused_signal(_request("same"))
    c = await client.get_fused_signal(_request("other"))
    assert a is not None and b is not None and c is not None
    assert a.signal_id == b.signal_id
    assert a == b
    assert a.signal_id != c.signal_id


async def test_api_version_and_explanation_ref() -> None:
    s = await StubFusionClient().get_fused_signal(_request())
    assert s is not None
    assert s.api_version == versions.API_VERSION
    assert s.explanation_ref == "fusion_stub"


async def test_output_passes_contract_and_ttl_is_half_bar() -> None:
    s = await StubFusionClient().get_fused_signal(_request())
    assert s is not None
    assert FusedSignal.model_validate(s.model_dump()) == s
    assert s.expires_at - s.timestamp == 30 * 60_000
    assert s.idempotency_key == "key-1" and s.asset == "BTC/USDT"
