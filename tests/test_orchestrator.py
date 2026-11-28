"""Минимальный оркестратор `augur run` (0.11.0, ARCH §2.12): бар → сигнал → Risk Gate → ордер."""

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, SecretStr

from augur.adapters.paper import PaperAdapter
from augur.config.settings import ModeSettings, RunSettings, Settings, StrategySpec, TradingMode
from augur.data.models import Bar
from augur.execution import ModeStore
from augur.notify import Notification
from augur.orchestrator import Runner
from augur.risk.post_trade import CBTrigger
from augur.strategies.base import Signal, SignalAction, Strategy
from augur.strategies.registry import register_strategy
from tests._live import FakeLiveVenue, live_settings
from tests.conftest import make_settings
from tests.test_signal_ready import NOW_MS

H = 3_600_000


class _NoParams(BaseModel):
    pass


@register_strategy
class AlwaysBuy(Strategy):
    name = "test_always_buy"
    version = "1.0.0"
    params_model = _NoParams

    @property
    def warmup_bars(self) -> int:
        return 2

    def on_bar(self, bar: Bar, history: Sequence[Bar]) -> Signal | None:
        return Signal(action=SignalAction.BUY, strength=0.8, reason="test", timestamp=bar.ts_open)


class Clock:
    def __init__(self, now: int = NOW_MS + 30 * H) -> None:
        self.now = now
        self.adapters: list[PaperAdapter] = []

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms
        for a in self.adapters:
            a.set_clock(self.now)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, msg: Notification) -> None:
        self.sent.append(msg)


RUN = RunSettings(
    strategies=[StrategySpec(name="test_always_buy")], history_bars=40, bar_close_delay_s=0
)


def _runner(
    db: sqlite3.Connection, cfg: Settings, clock: Clock, **kw: Any
) -> tuple[Runner, PaperAdapter, FakeNotifier]:
    data = PaperAdapter(clock_ms=clock.now)
    clock.adapters.append(data)
    notifier = FakeNotifier()

    async def no_sleep(_: float) -> None:
        return None

    r = Runner(db, cfg, clock=clock, data_adapter=data, notifier=notifier, sleep=no_sleep, **kw)
    return r, data, notifier


def _decisions(db: sqlite3.Connection) -> list[tuple[str, str]]:
    return db.execute(
        "SELECT mode, risk_verdict FROM decision_audit ORDER BY created_at"
    ).fetchall()


def _events(db: sqlite3.Connection, event: str) -> int:
    return int(db.execute("SELECT COUNT(*) FROM audit_log WHERE event = ?", (event,)).fetchone()[0])


async def test_paper_bar_to_order_and_no_double_processing(db: sqlite3.Connection) -> None:
    clock = Clock()
    cfg = make_settings(run=RUN)
    r, _, _ = _runner(db, cfg, clock)
    await r.run_forever(max_ticks=2)  # второй тик — тот же бар
    assert _events(db, "RUN_STARTED") == 1 and _events(db, "RUN_STOPPED") == 1
    assert len(_decisions(db)) == 1
    orders = db.execute("SELECT mode, state FROM orders").fetchall()
    assert orders == [("PAPER", "FILLED")]
    assert db.execute("SELECT COUNT(*) FROM ohlcv WHERE venue = 'paper'").fetchone()[0] >= 39

    # рестарт на том же баре не даёт второго решения
    r2, _, _ = _runner(db, cfg, clock)
    await r2.run_forever(max_ticks=1)
    assert len(_decisions(db)) == 1

    # следующий бар обрабатывается
    clock.advance(H)
    r3, _, _ = _runner(db, cfg, clock)
    await r3.run_forever(max_ticks=1)
    assert len(_decisions(db)) == 2


async def test_analysis_only_computes_no_signals(db: sqlite3.Connection) -> None:
    clock = Clock()
    cfg = make_settings(run=RUN)
    ModeStore(db, configured_mode=TradingMode.PAPER).set_mode(
        TradingMode.ANALYSIS_ONLY, actor="t", reason="t", ts=clock.now
    )
    db.commit()
    r, _, _ = _runner(db, cfg, clock)
    await r.run_forever(max_ticks=1)
    assert _decisions(db) == []
    assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0


async def test_alerts_dispatched_from_cb_trip(db: sqlite3.Connection) -> None:
    clock = Clock()
    cfg = make_settings(run=RUN)
    r, _, notifier = _runner(db, cfg, clock)
    await r.start()
    await r.tick()
    assert r.paper is not None
    await r.paper.breaker.trip(CBTrigger.MANUAL, "test")
    clock.advance(cfg.run.alerts_s * 1000)
    await r.tick()
    kinds = [n.kind for n in notifier.sent]
    assert "CB_TRIPPED" in kinds
    await r.stop(reason="test")


async def test_full_auto_routes_to_live_without_approval(db: sqlite3.Connection) -> None:
    clock = Clock()
    cfg = live_settings(
        trading_mode=TradingMode.FULL_AUTO,
        max_allowed_mode=TradingMode.FULL_AUTO,
        telegram_bot_token=SecretStr("1:x"),
        telegram_chat_id="1",
        mode=ModeSettings(),
        run=RUN,
    )
    store = ModeStore(db, configured_mode=TradingMode.FULL_AUTO)
    store.write(TradingMode.FULL_AUTO, actor="t", reason="setup", ts=clock.now)
    db.commit()

    class Venue(FakeLiveVenue):
        pass

    venue = Venue(_ClockShim(clock))
    clock.adapters.append(venue)
    r, _, _ = _runner(db, cfg, clock, live_adapter=venue)
    await r.start()
    await r.tick()
    assert r.live is not None and r.live.orders.mode == TradingMode.FULL_AUTO
    assert venue.place_calls == 1
    assert db.execute("SELECT mode, state FROM orders").fetchall() == [("FULL_AUTO", "FILLED")]
    assert _events(db, "ORDER_APPROVAL_REQUIRED") == 0

    # CB → PAPER-hold: live-стек остаётся на книге FULL_AUTO, новые сигналы идут в PAPER
    await r.live.breaker.trip(CBTrigger.MANUAL, "test")
    clock.advance(H)
    await r.tick()
    assert r.live is not None and r.live.orders.mode == TradingMode.FULL_AUTO
    assert venue.place_calls == 1
    modes = [m for m, _ in _decisions(db)]
    assert modes == ["FULL_AUTO", "PAPER"]
    await r.stop(reason="test")


class _ClockShim:
    """FakeLiveVenue ждёт объект с `.now`."""

    def __init__(self, clock: Clock) -> None:
        self._c = clock

    @property
    def now(self) -> int:
        return self._c.now


async def test_job_failure_is_audited_once(db: sqlite3.Connection) -> None:
    clock = Clock()
    cfg = make_settings(run=RUN)
    r, data, _ = _runner(db, cfg, clock)

    async def boom(*_: object) -> list[Bar]:
        raise RuntimeError("feed down")

    data.get_historical = boom  # type: ignore[method-assign]
    await r.start()
    await r.tick()
    clock.advance(60_000)
    await r.tick()
    assert _events(db, "RUN_ERROR") == 1  # не чаще раза в 15 минут на задачу
    clock.advance(15 * 60_000)
    await r.tick()
    assert _events(db, "RUN_ERROR") == 2
    payload = db.execute("SELECT payload_json FROM audit_log WHERE event='RUN_ERROR'").fetchone()
    assert "RuntimeError" in payload[0]
    await r.stop(reason="test")


async def test_calendar_warning_and_blackout_blocks_entry(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """0.12.0: нет календаря → MACRO_CALENDAR_MISSING; событие в окне → R15 отклоняет BUY."""
    from augur.config.settings import MacroSettings

    clock = Clock()
    r, _, _ = _runner(db, make_settings(run=RUN), clock)
    await r.run_forever(max_ticks=1)
    assert _events(db, "MACRO_CALENDAR_MISSING") == 1

    when = datetime.fromtimestamp((clock.now + H + 5 * 60_000) / 1000, tz=UTC).isoformat()
    cal = tmp_path / "cal.yaml"
    cal.write_text(
        f"events:\n  - id: evt_cpi\n    name: CPI\n    time: '{when}'\n    impact: high\n"
        "    scope: ['*']\n",
        encoding="utf-8",
    )
    clock.advance(H)
    cfg = make_settings(run=RUN, macro=MacroSettings(calendar_path=str(cal)))
    r2, _, _ = _runner(db, cfg, clock)
    await r2.run_forever(max_ticks=1)
    assert _events(db, "MACRO_CALENDAR_MISSING") == 1
    last = db.execute(
        "SELECT risk_verdict, risk_reason FROM decision_audit ORDER BY created_at DESC, id DESC"
    ).fetchone()
    assert last[0] == "REJECTED" and "evt_cpi" in last[1]


async def test_fusion_jobs_skipped_with_external_endpoint(db: sqlite3.Connection) -> None:
    """С FUSION__ENDPOINT задачи Fusion (nightly, LLM) ведёт `augur fusion serve`."""
    from augur.config.settings import FusionSettings
    from augur.fusion.client import StubFusionClient

    clock = Clock(NOW_MS + 30 * H)
    clock.now = clock.now - clock.now % (24 * H) + 3 * H + 30 * H  # после nightly_hour_utc
    local = make_settings(run=RUN, fusion=FusionSettings(enabled=True))
    r, _, _ = _runner(db, local, clock, fusion_client=StubFusionClient())
    await r.run_forever(max_ticks=1)
    assert _events(db, "ML_MONITOR_REPORT") == 1  # in-process: nightly здесь

    clock.advance(24 * H)
    remote = make_settings(
        run=RUN, fusion=FusionSettings(enabled=True, endpoint="http://127.0.0.1:8100")
    )
    r2, _, _ = _runner(db, remote, clock, fusion_client=StubFusionClient())
    await r2.run_forever(max_ticks=1)
    assert _events(db, "ML_MONITOR_REPORT") == 1
