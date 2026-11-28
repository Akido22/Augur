"""`FusionJobs` (0.12.0): nightly по расписанию, LLM по событию календаря; HOLD при breaker."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from augur.config.settings import FusionSettings, LLMProvider, LLMSettings, MacroSettings
from augur.fusion.client import FusionRequest
from augur.fusion.engine import verify
from augur.fusion.jobs import FusionJobs
from augur.fusion.local import LocalFusionClient
from augur.strategies.base import SignalAction
from tests.conftest import make_settings
from tests.fusion.llm._fake import FakeLLM, ctx_json

DAY0 = int(datetime(2026, 11, 20, tzinfo=UTC).timestamp() * 1000)
H = 3_600_000
PROVIDERS = [LLMProvider(name="p", base_url="https://p.example/v1", model="m")]


class Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


async def test_nightly_once_per_day_after_hour(db: sqlite3.Connection) -> None:
    clock = Clock(DAY0 + 1 * H)
    s = make_settings(fusion=FusionSettings(enabled=True), symbols=["BTC/USDT"])
    jobs = FusionJobs(db, s, clock=clock)
    assert jobs.nightly_enabled
    assert await jobs.nightly() == []  # 01:00 < nightly_hour_utc=2
    clock.now = DAY0 + 3 * H
    reports = await jobs.nightly()
    assert [r.status for r in reports] == ["no_model"]
    assert await jobs.nightly() == []  # уже был отчёт сегодня
    clock.now = DAY0 + 27 * H  # следующие сутки
    assert len(await jobs.nightly()) == 1
    assert len(await jobs.nightly(force=True)) == 1


async def test_tick_runs_nightly_only_when_fusion_enabled(db: sqlite3.Connection) -> None:
    clock = Clock(DAY0 + 3 * H)
    await FusionJobs(db, make_settings(), clock=clock).tick()
    assert db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
    await FusionJobs(db, make_settings(fusion=FusionSettings(enabled=True)), clock=clock).tick()
    events = [r[0] for r in db.execute("SELECT event FROM audit_log")]
    assert events == ["ML_MONITOR_REPORT"]


async def test_llm_pre_event_refresh_once(db: sqlite3.Connection, tmp_path: Path) -> None:
    now = DAY0 + 13 * H
    when = datetime.fromtimestamp((now + 20 * 60_000) / 1000, tz=UTC).isoformat()
    cal = tmp_path / "cal.yaml"
    cal.write_text(
        f"events:\n  - id: evt_cpi\n    name: CPI\n    time: '{when}'\n    impact: high\n"
        "    scope: [USD]\n",
        encoding="utf-8",
    )
    s = make_settings(
        fusion=FusionSettings(enabled=True, llm=LLMSettings(mode="enabled", providers=PROVIDERS)),
        macro=MacroSettings(calendar_path=str(cal)),
        symbols=["BTC/USDT", "ETH/USDT"],
    )
    as_of = datetime.fromtimestamp(now / 1000, tz=UTC).isoformat().replace("+00:00", "Z")
    fake = FakeLLM([lambda _s, u: ctx_json(0.0, asset=_asset(u), as_of=as_of)])
    jobs = FusionJobs(db, s, clock=Clock(now), llm_client=fake)
    done = await jobs.llm_pre_event()
    assert sorted(done) == [("BTC/USDT", "evt_cpi"), ("ETH/USDT", "evt_cpi")]
    assert await jobs.llm_pre_event() == []
    rows = db.execute("SELECT asset, status FROM llm_contexts ORDER BY asset").fetchall()
    assert rows == [("BTC/USDT", "ok"), ("ETH/USDT", "ok")]
    assert all('id="evt_cpi"' in c["user"] for c in fake.calls)


def _asset(user: str) -> str:
    return user.split('asset="', 1)[1].split('"', 1)[0]


async def test_breaker_open_gives_valid_hold_without_ml(db: sqlite3.Connection) -> None:
    """§4.3 п. 1: CB TRIPPED → валидный HOLD; ML не считается (раньше verify() отклонял)."""

    class _NoML:
        def predict(self, *_a: object) -> None:
            raise AssertionError("ML must not run while breaker is open")

    client = LocalFusionClient(
        db,
        FusionSettings(enabled=True),
        ml=_NoML(),  # type: ignore[arg-type]
        llm=None,
        clock=lambda: DAY0 + 1_000,
        breaker_open=lambda: True,
    )
    req = FusionRequest(
        asset="BTC/USDT",
        timeframe="1h",
        timestamp=DAY0,
        idempotency_key="k" * 64,
        context={"action": "BUY", "strength": 0.9},
    )
    sig = await client.get_fused_signal(req)
    assert sig is not None and sig.action == SignalAction.HOLD
    assert sig.reason == "circuit_breaker_open"
    assert verify(sig, None) is sig
