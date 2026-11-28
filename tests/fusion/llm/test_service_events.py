"""LLM-контекст с макро-календарём (0.12.0; FUSION SPEC §3.1, §3.3, §5.1–5.2)."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from augur.config.settings import LLMProvider, LLMSettings, MacroSettings
from augur.data.calendar import MacroCalendar, MacroEvent
from augur.fusion.llm.inputs import (
    EventInput,
    PriceSummary,
    imminent_event_ids,
    load_event_inputs,
)
from augur.fusion.llm.service import LLMContextService
from tests.conftest import make_settings
from tests.fusion.llm._fake import FakeLLM, ctx_json

AS_OF = int(datetime(2026, 11, 20, 8, tzinfo=UTC).timestamp() * 1000)
PRICE = PriceSummary(window_h=6, ret=0.0, vol_z=0.0)
PROVIDERS = [LLMProvider(name="p", base_url="https://p.example/v1", model="m")]
MIN = 60_000


def _event(eid: str, delta_min: int, impact: str = "high", scope: str = "USD") -> MacroEvent:
    return MacroEvent(
        id=eid,
        name=f"Event {eid}",
        time=datetime.fromtimestamp((AS_OF + delta_min * MIN) / 1000, tz=UTC),
        impact=impact,  # type: ignore[arg-type]
        scope=[scope],
    )


def _svc(db: sqlite3.Connection, fake: FakeLLM) -> LLMContextService:
    return LLMContextService(
        db,
        LLMSettings(mode="enabled", providers=PROVIDERS),
        fake,  # type: ignore[arg-type]
        {},
        clock=lambda: AS_OF + 5,
    )


def _inputs(*events: MacroEvent) -> tuple[list[EventInput], set[str]]:
    cal = MacroCalendar(events=list(events))
    ev = load_event_inputs(
        cal, asset="BTC/USDT", as_of=AS_OF, horizon_h=24, lookback_min=15, aliases={"USDT": "USD"}
    )
    return ev, imminent_event_ids(ev, as_of=AS_OF, window_min=60, lookback_min=15)


def test_event_inputs_scope_horizon_and_imminent() -> None:
    ev, imminent = _inputs(
        _event("evt_cpi", 25),
        _event("evt_fomc", 600),
        _event("evt_far", 60 * 30),  # за горизонтом 24 ч
        _event("evt_jpy", 30, scope="JPY"),
        _event("evt_med", 20, impact="medium"),
        _event("evt_past", -30),  # раньше lookback
    )
    assert [e.event_id for e in ev] == ["evt_med", "evt_cpi", "evt_fomc"]
    assert imminent == {"evt_cpi"}  # high и в окне 60 мин


async def test_events_rendered_and_grounded_flag_accepted(db: sqlite3.Connection) -> None:
    ev, imminent = _inputs(_event("evt_cpi", 25))
    flag = [{"code": "high_impact_event_imminent", "severity": "high", "evidence_ids": ["evt_cpi"]}]
    fake = FakeLLM([ctx_json(-0.1, flags=flag)])
    res = await _svc(db, fake).refresh(
        "BTC/USDT", as_of=AS_OF, news=[], price=PRICE, events=ev, imminent_ids=imminent
    )
    assert res.status == "ok" and res.context is not None
    assert [(f.code.value, f.severity.value) for f in res.context.risk_flags] == [
        ("high_impact_event_imminent", "high")
    ]
    assert len(fake.calls) == 3  # high-impact событие → всегда N сэмплов (§5.2)
    assert (
        '<event id="evt_cpi"' in fake.calls[0]["user"] and 'impact="high"' in fake.calls[0]["user"]
    )


async def test_imminent_flag_for_distant_event_rejected(db: sqlite3.Connection) -> None:
    ev, imminent = _inputs(_event("evt_fomc", 600))  # в горизонте, но не «imminent»
    assert imminent == set()
    flag = [
        {"code": "high_impact_event_imminent", "severity": "high", "evidence_ids": ["evt_fomc"]}
    ]
    fake = FakeLLM([ctx_json(-0.1, flags=flag)])
    res = await _svc(db, fake).refresh(
        "BTC/USDT", as_of=AS_OF, news=[], price=PRICE, events=ev, imminent_ids=imminent
    )
    assert res.status != "ok" and res.context is None
    statuses = {r[0] for r in db.execute("SELECT status FROM llm_calls")}
    assert statuses == {"ungrounded"}
    assert len(fake.calls) == 2  # один сэмпл (нет imminent) + одна попытка повтора


async def test_unknown_event_id_is_ungrounded(db: sqlite3.Connection) -> None:
    ev, imminent = _inputs(_event("evt_cpi", 25))
    flag = [{"code": "earnings_window", "severity": "medium", "evidence_ids": ["evt_invented"]}]
    fake = FakeLLM([ctx_json(0.0, flags=flag)])
    res = await _svc(db, fake).refresh(
        "BTC/USDT", as_of=AS_OF, news=[], price=PRICE, events=ev, imminent_ids=imminent
    )
    assert res.context is None


async def test_refresh_llm_context_reads_calendar(db: sqlite3.Connection, tmp_path: Path) -> None:
    from augur.config.settings import FusionSettings
    from augur.fusion.factory import refresh_llm_context

    cal = tmp_path / "cal.yaml"
    when = datetime.fromtimestamp((AS_OF + 20 * MIN) / 1000, tz=UTC).isoformat()
    cal.write_text(
        f"events:\n  - id: evt_cpi\n    name: US CPI\n    time: '{when}'\n"
        "    impact: high\n    scope: [USD]\n",
        encoding="utf-8",
    )
    settings = make_settings(
        fusion=FusionSettings(enabled=True, llm=LLMSettings(mode="enabled", providers=PROVIDERS)),
        macro=MacroSettings(calendar_path=str(cal)),
    )
    flag = [{"code": "high_impact_event_imminent", "severity": "high", "evidence_ids": ["evt_cpi"]}]
    fake = FakeLLM([ctx_json(0.0, flags=flag, as_of="2026-11-20T08:00:00Z")])
    res = await refresh_llm_context(
        db,
        settings,
        symbol="BTC/USDT",
        timeframe="1h",
        clock=lambda: AS_OF,
        client=fake,  # type: ignore[arg-type]
    )
    assert res.status == "ok" and res.context is not None
    assert res.context.risk_flags[0].code.value == "high_impact_event_imminent"
