"""Модели новостного контура (ARCHITECTURE.md §8): сырые и нормализованные новости, скор,
контракт `news_event/1.0` (§8.3).

Время — aware UTC `datetime` в моделях, Unix ms UTC в SQLite.
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "NEWS_EVENT_SCHEMA_ID",
    "Importance",
    "NewsEvent",
    "NewsItem",
    "RawNewsItem",
    "SentimentScore",
]

Importance = Literal["low", "medium", "high"]
NEWS_EVENT_SCHEMA_ID = "news_event/1.0"


def _utc(v: datetime) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return v.astimezone(UTC)


class RawNewsItem(BaseModel):
    """Новость как её отдал источник (после разбора JSON, до нормализации)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1)
    external_id: str | None = None
    url: str | None = None
    title: str
    summary: str | None = None
    published_at: datetime
    related: list[str] = Field(default_factory=list)  # тикеры источника (Finnhub `related`)
    source_domain: str | None = None  # издатель (Finnhub `source`), часть content_hash

    @field_validator("published_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _utc(v)


class NewsItem(BaseModel):
    """Нормализованная новость (FUSION SPEC [Д4]): текст очищен и обрезан, символы привязаны."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1)
    external_id: str | None = None
    url: str | None = None
    content_hash: str = Field(min_length=64, max_length=64)
    title: str = Field(min_length=1)
    summary: str | None = None
    published_at: datetime
    symbols: list[str] = Field(default_factory=list)

    @field_validator("published_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _utc(v)


class SentimentScore(BaseModel):
    """Результат `SentimentScorer.score` (ARCHITECTURE.md §8.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sentiment: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    importance: Importance
    method: str = Field(min_length=1)  # "rule:v1"


class NewsEvent(BaseModel):
    """Контракт `news_event/1.0` (ARCHITECTURE.md §8.3). Не создаёт ордеров и сигналов на вход."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1)
    news_id: str = Field(min_length=1)
    symbols: list[str]
    published_at: datetime
    sentiment: float = Field(ge=-1.0, le=1.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    sentiment_method: str = Field(min_length=1)
    source: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=300)
    importance: Importance | None = None

    @field_validator("published_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _utc(v)
