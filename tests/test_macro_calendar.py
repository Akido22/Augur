"""Макро-календарь (0.12.0): YAML → схема, выбор событий, блокировка входов Risk Gate (R15)."""

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from augur.config.settings import MacroSettings, RiskLimits, TradingMode
from augur.data.calendar import CalendarError, MacroCalendar, load_calendar, symbol_currencies
from augur.risk.context import MACRO_FLAG, MACRO_INVALID_FLAG, RiskContextBuilder
from augur.risk.gate import RiskGate
from augur.strategies.base import SignalAction
from tests._risk import NOW, make_ctx, make_signal

NOW_MS = int(NOW.timestamp() * 1000)


def _iso(delta: timedelta) -> str:
    return (NOW + delta).isoformat().replace("+00:00", "Z")


def write_calendar(path: Path, *events: dict[str, object]) -> Path:
    lines = ["events:"]
    for e in events:
        lines.append(f"  - id: {e['id']}")
        lines.append(f"    name: {e.get('name', 'Event')}")
        lines.append(f"    time: {e['time']}")
        lines.append(f"    impact: {e.get('impact', 'high')}")
        lines.append(f"    scope: {json.dumps(e.get('scope', ['USD']))}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_calendar(tmp_path / "nope.yaml").events == []


def test_load_and_select_by_scope_and_alias(tmp_path: Path) -> None:
    p = write_calendar(
        tmp_path / "cal.yaml",
        {"id": "evt_cpi", "time": _iso(timedelta(minutes=10)), "scope": ["USD"]},
        {"id": "evt_ecb", "time": _iso(timedelta(minutes=20)), "scope": ["EUR"]},
        {"id": "evt_all", "time": _iso(timedelta(hours=2)), "impact": "medium", "scope": ["*"]},
    )
    cal = load_calendar(p)
    aliases = {"USDT": "USD"}
    assert symbol_currencies("BTC/USDT", aliases) >= {"BTC", "USDT", "USD"}
    window = cal.select(
        start_ms=NOW_MS, end_ms=NOW_MS + 3 * 3_600_000, symbol="BTC/USDT", aliases=aliases
    )
    assert [e.id for e in window] == ["evt_cpi", "evt_all"]
    assert [
        e.id for e in cal.select(start_ms=NOW_MS, end_ms=NOW_MS + 3_600_000, symbol="EUR/USD")
    ] == ["evt_cpi", "evt_ecb"]
    high = cal.select(start_ms=NOW_MS, end_ms=NOW_MS + 3 * 3_600_000, impacts=["high"])
    assert {e.id for e in high} == {"evt_cpi", "evt_ecb"}


@pytest.mark.parametrize(
    "body",
    [
        "events:\n  - id: a\n    name: x\n    time: 2026-12-10T13:30:00\n    impact: high\n"
        "    scope: [USD]\n",  # без часового пояса
        "events:\n  - id: a\n    name: x\n    time: 2026-12-10T13:30:00Z\n    impact: huge\n"
        "    scope: [USD]\n",
        "events:\n  - id: a\n    name: x\n    time: 2026-12-10T13:30:00Z\n    impact: high\n"
        "    scope: [USD]\n  - id: a\n    name: y\n    time: 2026-12-11T13:30:00Z\n"
        "    impact: high\n    scope: [USD]\n",  # дубликат id
        "events: [unclosed\n",  # YAML
        "- just a list\n",
        "events:\n  - id: a\n    name: x\n    time: 2026-12-10T13:30:00Z\n    impact: high\n"
        "    scope: [USD]\n    extra: 1\n",
    ],
)
def test_invalid_calendar_rejected(tmp_path: Path, body: str) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(CalendarError):
        load_calendar(p)


def test_reload_on_change(tmp_path: Path) -> None:
    p = write_calendar(tmp_path / "c.yaml", {"id": "a", "time": _iso(timedelta(hours=1))})
    assert [e.id for e in load_calendar(p).events] == ["a"]
    write_calendar(
        p,
        {"id": "a", "time": _iso(timedelta(hours=1))},
        {"id": "bb", "time": _iso(timedelta(hours=2))},
    )
    assert [e.id for e in load_calendar(p).events] == ["a", "bb"]


def _builder(db: sqlite3.Connection, path: Path, **m: object) -> RiskContextBuilder:
    return RiskContextBuilder(
        db,
        configured_mode=TradingMode.PAPER,
        macro=MacroSettings(calendar_path=str(path), **m),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("delta", "blocked"),
    [
        (timedelta(minutes=14), True),  # за 14 мин до события
        (timedelta(minutes=-14), True),  # 14 мин после
        (timedelta(minutes=16), False),
        (timedelta(minutes=-16), False),
    ],
)
def test_blackout_window(
    db: sqlite3.Connection, tmp_path: Path, delta: timedelta, blocked: bool
) -> None:
    p = write_calendar(tmp_path / "c.yaml", {"id": "evt_cpi", "time": _iso(delta)})
    flags, ids = _builder(db, p).macro_effects("BTC/USDT", NOW)
    assert (flags != ()) is blocked
    if blocked:
        assert flags[0].code == MACRO_FLAG and flags[0].severity == "high"
        assert ids == ("evt_cpi",)


def test_non_blocking_impact_and_other_scope_ignored(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    p = write_calendar(
        tmp_path / "c.yaml",
        {"id": "evt_med", "time": _iso(timedelta(minutes=5)), "impact": "medium"},
        {"id": "evt_jpy", "time": _iso(timedelta(minutes=5)), "scope": ["JPY"]},
    )
    assert _builder(db, p).macro_effects("BTC/USDT", NOW) == ((), ())
    flags, _ = _builder(db, p, blocking_impacts=["medium", "high"]).macro_effects("BTC/USDT", NOW)
    assert flags and flags[0].code == MACRO_FLAG


def test_invalid_calendar_fails_closed(db: sqlite3.Connection, tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("events: [oops\n", encoding="utf-8")
    flags, ids = _builder(db, p).macro_effects("BTC/USDT", NOW)
    assert [(f.code, f.severity) for f in flags] == [(MACRO_INVALID_FLAG, "high")] and ids == ()


def test_build_puts_macro_flags_into_context(db: sqlite3.Connection, tmp_path: Path) -> None:
    from augur.risk.context import MarketSnapshot

    p = write_calendar(tmp_path / "c.yaml", {"id": "evt_cpi", "time": _iso(timedelta(minutes=5))})
    ctx = _builder(db, p).build(make_signal(), MarketSnapshot(), now=NOW)
    assert ctx.macro_event_ids == ("evt_cpi",) and ctx.macro_flags[0].code == MACRO_FLAG


def _r15(action: SignalAction, **ctx: object):  # type: ignore[no-untyped-def]
    verdict = RiskGate(RiskLimits()).evaluate_sync(make_signal(action), make_ctx(**ctx))
    return next(c for c in verdict.checks if c.name == "risk_flags")


def test_gate_r15_blocks_entry_but_not_close() -> None:
    from augur.fusion.contract import RiskFlag

    flags = (RiskFlag(code=MACRO_FLAG, severity="high"),)
    entry = _r15(SignalAction.BUY, macro_flags=flags, macro_event_ids=("evt_cpi",))
    assert not entry.passed
    assert "high_impact_event_imminent" in (entry.reason or "")
    assert "evt_cpi" in (entry.reason or "")
    close = _r15(
        SignalAction.CLOSE,
        macro_flags=flags,
        macro_event_ids=("evt_cpi",),
        positions={"BTC/USDT": __import__("decimal").Decimal("1")},
    )
    assert close.passed
    assert _r15(SignalAction.BUY).passed


def test_default_calendar_is_valid_example() -> None:
    """Пример в репозитории проходит схему (то, что копирует человек)."""
    example = Path(__file__).resolve().parents[1] / "config" / "macro_calendar.example.yaml"
    cal = load_calendar(example)
    assert isinstance(cal, MacroCalendar) and cal.events
