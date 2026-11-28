"""Входы LLM-контекста: новости актива за окно, сводка цены (БД) и события макро-календаря
(YAML, 0.12.0) (FUSION SPEC §3.1, §3.3).

В промпт не попадают ML-прогноз, технический сигнал, позиция и P&L (§4.4) — их здесь нет.
"""

import math
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from augur.data.calendar import MacroCalendar
from augur.data.models import timeframe_ms
from augur.fusion.llm.sanitize import clean_text

__all__ = [
    "EventInput",
    "NewsInput",
    "PriceSummary",
    "imminent_event_ids",
    "load_event_inputs",
    "load_news_inputs",
    "load_price_summary",
]


class NewsInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    news_id: str
    published_ts: int
    source: str
    title: str
    body: str

    @property
    def published_iso(self) -> str:
        return datetime.fromtimestamp(self.published_ts / 1000, tz=UTC).isoformat()


class EventInput(BaseModel):
    """Событие календаря в промпте (`<macro>`): id — evidence для `risk_flags`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    time_ms: int
    time: str  # ISO UTC
    impact: str
    name: str


def load_event_inputs(
    calendar: MacroCalendar,
    *,
    asset: str,
    as_of: int,
    horizon_h: int,
    lookback_min: int,
    aliases: Mapping[str, str],
) -> list[EventInput]:
    """События, затронувшие актив, в [as_of − lookback, as_of + horizon], по времени."""
    events = calendar.select(
        start_ms=as_of - lookback_min * 60_000,
        end_ms=as_of + horizon_h * 3_600_000,
        symbol=asset,
        aliases=aliases,
    )
    return [
        EventInput(
            event_id=e.id,
            time_ms=e.time_ms,
            time=e.time_iso,
            impact=e.impact,
            name=clean_text(e.name, 200),
        )
        for e in events
    ]


def imminent_event_ids(
    events: list[EventInput], *, as_of: int, window_min: int, lookback_min: int
) -> set[str]:
    """id событий, для которых допустим `high_impact_event_imminent` (FUSION SPEC §5.1)."""
    return {
        e.event_id
        for e in events
        if e.impact == "high"
        and as_of - lookback_min * 60_000 <= e.time_ms <= as_of + window_min * 60_000
    }


class PriceSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window_h: int
    ret: float
    vol_z: float


def load_news_inputs(
    conn: sqlite3.Connection, *, asset: str, as_of: int, window_h: int, limit: int, body_chars: int
) -> list[NewsInput]:
    """Новости, привязанные к `asset`, опубликованные в (as_of − window, as_of], по времени."""
    rows = conn.execute(
        "SELECT id, published_ts, source, title, summary FROM news "
        "WHERE published_ts > ? AND published_ts <= ? "
        "AND EXISTS (SELECT 1 FROM json_each(news.symbols_json) WHERE value = ?) "
        "ORDER BY published_ts DESC, id DESC LIMIT ?",
        (as_of - window_h * 3_600_000, as_of, asset, limit),
    ).fetchall()
    return [
        NewsInput(
            news_id=str(r[0]),
            published_ts=int(r[1]),
            source=clean_text(r[2], 40),
            title=clean_text(r[3], 300),
            body=clean_text(r[4], body_chars),
        )
        for r in reversed(rows)
    ]


def load_price_summary(
    conn: sqlite3.Connection, *, venue: str, asset: str, timeframe: str, as_of: int, window_h: int
) -> PriceSummary:
    """Доходность за окно и z-score объёма последнего бара (по 20 барам). Нет данных → нули."""
    tf = timeframe_ms(timeframe)
    n = max(2, (window_h * 3_600_000) // tf + 1)
    rows = conn.execute(
        "SELECT close, volume FROM ohlcv WHERE venue=? AND symbol=? AND timeframe=? "
        "AND ts_open <= ? ORDER BY ts_open DESC LIMIT ?",
        (venue, asset, timeframe, as_of - tf, max(n, 20)),
    ).fetchall()
    if len(rows) < 2:
        return PriceSummary(window_h=window_h, ret=0.0, vol_z=0.0)
    closes = [float(r[0]) for r in rows]
    vols = [float(r[1]) for r in rows[:20]]
    first = closes[min(n, len(closes)) - 1]
    ret = closes[0] / first - 1.0 if first > 0 else 0.0
    mean = sum(vols) / len(vols)
    sd = math.sqrt(sum((v - mean) ** 2 for v in vols) / len(vols))
    return PriceSummary(
        window_h=window_h,
        ret=round(ret, 6),
        vol_z=round((vols[0] - mean) / sd, 4) if sd > 0 else 0.0,
    )
