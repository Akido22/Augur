"""Общие помощники тестов Execution: PAPER-стек на in-memory БД с управляемыми часами."""

import sqlite3
from typing import Any

from augur.adapters.paper import PaperAdapter
from augur.backtest.costs import CostModel
from augur.config.settings import Settings
from augur.data.models import Bar, timeframe_ms
from augur.execution import PaperStack, build_paper_stack
from augur.fusion import StubFusionClient
from augur.signals import SignalEngine, SignalReady, SignalRepository
from augur.strategies.base import SignalAction
from tests.conftest import make_settings
from tests.test_signal_ready import NOW_MS, _ss

APP = "0.6.0"


class Clock:
    """Управляемые часы, Unix ms. Двигает и `PaperAdapter.clock_ms`, если он передан."""

    def __init__(self, now: int = NOW_MS, broker: PaperAdapter | None = None) -> None:
        self.now = now
        self.broker = broker

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms
        if self.broker is not None:
            self.broker.set_clock(self.now)


def settings(**over: Any) -> Settings:
    """Настройки по умолчанию (PAPER), без .env."""
    return make_settings(**over)


def stack(
    db: sqlite3.Connection,
    *,
    clock: Clock | None = None,
    cfg: Settings | None = None,
    broker: PaperAdapter | None = None,
    restore: bool = True,
) -> tuple[PaperStack, Clock]:
    clock = clock or Clock()
    cfg = cfg or settings()
    ex = cfg.execution
    broker = broker or PaperAdapter(
        clock_ms=clock.now,
        start_cash=ex.paper_start_cash,
        costs=CostModel(commission_pct=ex.commission_pct, slippage_pct=ex.slippage_pct),
    )
    clock.broker = broker
    st = build_paper_stack(db, cfg, app_version=APP, clock=clock, broker=broker, restore=restore)
    return st, clock


async def classic_signal(
    db: sqlite3.Connection,
    action: SignalAction = SignalAction.BUY,
    *,
    now_ms: int = NOW_MS,
    bar_ts: int | None = None,
) -> SignalReady:
    """Стратегический + итоговый классический сигнал, записанные в `signals`."""
    repo = SignalRepository(db, config_hash="c" * 64, code_version=APP)
    eng = SignalEngine(StubFusionClient(), clock=lambda: now_ms, repo=repo)
    kw = {} if bar_ts is None else {"ts": bar_ts}
    sig = await eng.resolve_signal([_ss(action, 0.7, **kw)])
    assert sig is not None
    return sig


def audit_events(db: sqlite3.Connection, event: str | None = None) -> list[str]:
    if event is None:
        return [r[0] for r in db.execute("SELECT event FROM audit_log ORDER BY id")]
    return [
        r[0] for r in db.execute("SELECT event FROM audit_log WHERE event=? ORDER BY id", (event,))
    ]


async def bars(st: PaperStack, clock: Clock, n: int = 30) -> list[Bar]:
    """Последние закрытые 1h-бары синтетики (для R7 abnormal_volatility)."""
    h = timeframe_ms("1h")
    end = clock.now // h * h
    return await st.broker.get_historical("BTC/USDT", "1h", end - n * h, end)


async def process(st: PaperStack, clock: Clock, sig: SignalReady) -> Any:
    return await st.pipeline.process(sig, bars=await bars(st, clock))
