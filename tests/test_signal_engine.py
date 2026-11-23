"""Signal Engine: generate_signals, ULID, хеши, process() с отбраковкой невалидного Fusion."""

import math
import sqlite3
import uuid

import pytest

from augur import versions
from augur.fusion import FusedSignal, FusionRequest, StubFusionClient
from augur.ids import is_ulid, new_ulid
from augur.signals import SignalEngine, StrategyContext, StrategySignal
from augur.signals.engine import data_hash, params_hash
from augur.strategies import get_strategy
from augur.strategies.base import Signal, SignalAction
from tests._bars import make_bars

CROCKFORD = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def _wave(n: int = 100) -> list[float]:
    return [100 + 10 * math.sin(i / 6) for i in range(n)]


def _engine(conn: sqlite3.Connection | None = None, now: int = 0) -> SignalEngine:
    return SignalEngine(StubFusionClient(), audit_conn=conn, clock=lambda: now)


def _sma_signals(n: int = 100) -> list[StrategySignal]:
    bars = make_bars(_wave(n))
    return _engine().generate_signals(get_strategy("sma_cross"), bars, "TEST/USD", "1h")


# --- generate_signals ------------------------------------------------------------


def test_generate_signals_sma_100_bars_non_empty() -> None:
    signals = _sma_signals()
    assert signals
    assert {s.action for s in signals} <= {SignalAction.BUY, SignalAction.CLOSE}
    assert all(s.strategy_version == "sma_cross@1.0.0" for s in signals)
    assert all(s.symbol == "TEST/USD" and s.timeframe == "1h" for s in signals)
    assert [s.timestamp for s in signals] == sorted(s.timestamp for s in signals)


def test_generate_signals_matches_on_bar_loop() -> None:
    """Обёртка не меняет решений стратегии: те же бары и действия, что у on_bar."""
    bars = make_bars(_wave())
    strat = get_strategy("sma_cross")
    expected = [
        (s.timestamp, s.action)
        for i, b in enumerate(bars)
        if (s := strat.on_bar(b, bars[:i])) is not None
    ]
    got = [(s.timestamp, s.action) for s in _sma_signals()]
    assert got == expected


def test_signal_id_is_ulid() -> None:
    signals = _sma_signals()
    for s in signals:
        assert len(s.signal_id) == 26
        assert set(s.signal_id) <= CROCKFORD
        assert is_ulid(s.signal_id)
        with pytest.raises(ValueError):
            uuid.UUID(s.signal_id)  # не UUID
    assert len({s.signal_id for s in signals}) == len(signals)


def test_new_ulid_encodes_timestamp() -> None:
    a, b = new_ulid(0), new_ulid((1 << 48) - 1)
    assert a.startswith("0000000000") and b.startswith("7ZZZZZZZZZ")
    assert new_ulid(1_000) < new_ulid(2_000)
    with pytest.raises(ValueError):
        new_ulid(1 << 48)


def test_params_hash_deterministic() -> None:
    assert params_hash({"short": 10, "long": 30}) == params_hash({"long": 30, "short": 10})
    assert params_hash({"short": 10, "long": 30}) != params_hash({"short": 5, "long": 30})
    s1 = get_strategy("sma_cross", {"short": 5, "long": 20})
    s2 = get_strategy("sma_cross", {"long": "20", "short": "5"})
    assert params_hash(s1.params) == params_hash(s2.params)
    hashes = {s.params_hash for s in _sma_signals()}
    assert hashes == {params_hash({"short": 10, "long": 30})}


def test_data_hash_depends_on_last_n_bars() -> None:
    strat = get_strategy("sma_cross", {"short": 2, "long": 3})  # warmup_bars = 4
    closes = [1.0, 2.0, 3.0, 4.0, 3.0, 2.0, 5.0, 1.0]
    bars = make_bars(closes)
    i = len(bars) - 1
    ctx = StrategyContext(
        bar=bars[i], history=bars[:i], symbol="TEST/USD", timeframe="1h", timestamp=bars[i].ts_open
    )
    signal = Signal(action=SignalAction.BUY, strength=1.0, reason="t", timestamp=ctx.timestamp)
    base = StrategySignal.from_signal(signal, strategy=strat, ctx=ctx)
    assert base.data_hash == data_hash(bars[-strat.warmup_bars :])

    # Бар вне окна последних N — хеш не меняется.
    outside = make_bars([99.0, *closes[1:]])
    ctx_out = ctx.model_copy(update={"history": outside[:i]})
    assert (
        StrategySignal.from_signal(signal, strategy=strat, ctx=ctx_out).data_hash == base.data_hash
    )

    # Бар внутри окна — хеш меняется.
    inside = make_bars([*closes[:-2], 9.0, closes[-1]])
    ctx_in = ctx.model_copy(update={"history": inside[:i]})
    assert (
        StrategySignal.from_signal(signal, strategy=strat, ctx=ctx_in).data_hash != base.data_hash
    )


def test_strategy_context_rejects_look_ahead() -> None:
    bars = make_bars([1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match="look-ahead"):
        StrategyContext(
            bar=bars[1], history=bars, symbol="TEST/USD", timeframe="1h", timestamp=bars[1].ts_open
        )


def test_generate_signals_wrapper_returns_empty_without_signal() -> None:
    bars = make_bars([1.0] * 40)
    strat = get_strategy("sma_cross")
    ctx = StrategyContext(
        bar=bars[-1],
        history=bars[:-1],
        symbol="TEST/USD",
        timeframe="1h",
        timestamp=bars[-1].ts_open,
    )
    assert strat.generate_signals(ctx) == []


# --- process ----------------------------------------------------------------------


def _audit_rows(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    return conn.execute(
        "SELECT event, severity, ref_id, payload_json FROM audit_log ORDER BY id"
    ).fetchall()


async def test_process_returns_valid_stub_signals(db: sqlite3.Connection) -> None:
    signals = _sma_signals()
    fused = await _engine(db, now=signals[0].timestamp).process(signals)
    assert len(fused) == len(signals)
    assert all(f.action is SignalAction.HOLD and f.explanation_ref == "fusion_stub" for f in fused)
    assert all(f.api_version == versions.API_VERSION for f in fused)
    assert _audit_rows(db) == []


class _BadApiVersionClient:
    """Клиент, собравший сигнал в обход валидации (model_construct) с чужим api_version."""

    async def get_fused_signal(self, request: FusionRequest) -> FusedSignal | None:
        good = await StubFusionClient().get_fused_signal(request)
        assert good is not None
        return FusedSignal.model_construct(**{**good.model_dump(), "api_version": "2.0"})


async def test_process_drops_api_version_mismatch_and_audits(db: sqlite3.Connection) -> None:
    signals = _sma_signals()[:1]
    engine = SignalEngine(_BadApiVersionClient(), audit_conn=db, clock=lambda: signals[0].timestamp)
    assert await engine.process(signals) == []
    rows = _audit_rows(db)
    assert len(rows) == 1
    event, severity, ref_id, payload = rows[0]
    assert (event, severity, ref_id) == ("FUSION_INVALID", "WARN", signals[0].signal_id)
    assert "api_version mismatch" in payload


async def test_process_drops_expired_signal(db: sqlite3.Connection) -> None:
    signals = _sma_signals()[:1]
    far_future = signals[0].timestamp + 10 * 3_600_000
    assert await _engine(db, now=far_future).process(signals) == []
    rows = _audit_rows(db)
    assert [r[0] for r in rows] == ["FUSION_INVALID"] and "expired" in rows[0][3]


@pytest.mark.parametrize(
    ("field", "value", "needle"),
    [("strength", 1.5, "strength"), ("explanation_ref", "", "explanation_ref")],
)
async def test_process_drops_other_invalid_fields(
    db: sqlite3.Connection, field: str, value: object, needle: str
) -> None:
    class _Client:
        async def get_fused_signal(self, request: FusionRequest) -> FusedSignal | None:
            good = await StubFusionClient().get_fused_signal(request)
            assert good is not None
            return FusedSignal.model_construct(**{**good.model_dump(), field: value})

    signals = _sma_signals()[:1]
    engine = SignalEngine(_Client(), audit_conn=db, clock=lambda: signals[0].timestamp)
    assert await engine.process(signals) == []
    assert needle in _audit_rows(db)[0][3]


async def test_process_fusion_exception_falls_back(db: sqlite3.Connection) -> None:
    class _Down:
        async def get_fused_signal(self, request: FusionRequest) -> FusedSignal | None:
            raise TimeoutError("fusion timeout")

    signals = _sma_signals()[:2]
    engine = SignalEngine(_Down(), audit_conn=db, clock=lambda: signals[0].timestamp)
    assert await engine.process(signals) == []
    assert [r[0] for r in _audit_rows(db)] == ["FUSION_FALLBACK", "FUSION_FALLBACK"]


async def test_process_without_audit_conn_still_drops() -> None:
    signals = _sma_signals()[:1]
    engine = SignalEngine(_BadApiVersionClient(), clock=lambda: signals[0].timestamp)
    assert await engine.process(signals) == []


def test_build_request_idempotency_key_is_stable() -> None:
    signals = _sma_signals()
    r1 = SignalEngine.build_request(signals[0])
    again = signals[0].model_copy(update={"signal_id": new_ulid()})
    assert SignalEngine.build_request(again).idempotency_key == r1.idempotency_key
    assert r1.timestamp == signals[0].timestamp + 3_600_000
    assert r1.context["strategy_signal_id"] == signals[0].signal_id
