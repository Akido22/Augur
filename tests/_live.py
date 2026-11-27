"""Помощники тестов MICRO_LIVE: «реальная» площадка = PaperAdapter с venue `ccxt:fake`."""

import sqlite3
from typing import Any

from augur.adapters import OrderAck, OrderRequest, PaperAdapter
from augur.config.settings import Settings, TradingMode
from augur.data.models import timeframe_ms
from augur.execution import LiveStack, ModeController, build_live_stack
from augur.signals import SignalReady
from tests._exec import APP, Clock, settings

LIVE_VENUE = "ccxt:fake"


class FakeLiveVenue(PaperAdapter):
    """Площадка с venue, отличным от `paper`; считает отправки."""

    def __init__(self, clock: Clock, **kw: Any) -> None:
        super().__init__(clock_ms=clock.now, **kw)
        self.venue = LIVE_VENUE
        self.place_calls = 0
        self.connected = False
        self.extra_positions: dict[str, Any] = {}

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def place_order(self, req: OrderRequest) -> OrderAck:
        self.place_calls += 1
        return await super().place_order(req)

    async def get_positions(self) -> Any:
        from augur.adapters import VenuePosition

        out = await super().get_positions()
        return [*out, *(VenuePosition(symbol=s, qty=q) for s, q in self.extra_positions.items())]


def live_settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "trading_mode": TradingMode.MICRO_LIVE,
        "max_allowed_mode": TradingMode.MICRO_LIVE,
    }
    base.update(over)
    return settings(**base)


def live_stack(
    db: sqlite3.Connection, *, clock: Clock | None = None, cfg: Settings | None = None
) -> tuple[LiveStack, Clock, FakeLiveVenue]:
    clock = clock or Clock()
    cfg = cfg or live_settings()
    venue = FakeLiveVenue(clock, start_cash=cfg.execution.paper_start_cash)
    clock.broker = venue
    st = build_live_stack(db, cfg, app_version=APP, clock=clock, adapter=venue)
    return st, clock, venue


async def enter_micro_live(
    db: sqlite3.Connection, clock: Clock, cfg: Settings | None = None
) -> ModeController:
    """PAPER → MICRO_LIVE через ModeController с кодом."""
    ctl = ModeController(db, settings=cfg or live_settings(), clock=clock)
    ticket = await ctl.request_change(TradingMode.MICRO_LIVE, actor="test")
    ctl.confirm_change(ticket.ticket_id, ticket.code, actor="test")
    assert ctl.current() == TradingMode.MICRO_LIVE
    return ctl


async def process_live(st: LiveStack, clock: Clock, sig: SignalReady) -> Any:
    h = timeframe_ms("1h")
    end = clock.now // h * h
    bars = await st.venue_adapter.get_historical("BTC/USDT", "1h", end - 30 * h, end)
    return await st.pipeline.process(sig, bars=bars)
