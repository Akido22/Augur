"""Таблица `news` (миграция 0006_news, ARCHITECTURE.md §3.2). Без COMMIT: транзакцией
управляет вызывающий (`NewsPipeline`)."""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from augur.ids import new_ulid
from augur.news.models import Importance, NewsItem, SentimentScore

__all__ = ["NewsRecord", "NewsRepository"]


@dataclass(frozen=True)
class NewsRecord:
    id: str
    source: str
    url: str | None
    title: str
    published_ts: int
    fetched_ts: int
    symbols: list[str]
    sentiment: float | None
    sentiment_method: str | None
    confidence: float | None
    importance: Importance | None


_COLS = (
    "id, source, url, title, published_ts, fetched_ts, symbols_json, sentiment, "
    "sentiment_method, confidence, importance"
)


def _record(row: Sequence[object]) -> NewsRecord:
    imp = row[10]
    return NewsRecord(
        id=str(row[0]),
        source=str(row[1]),
        url=None if row[2] is None else str(row[2]),
        title=str(row[3]),
        published_ts=int(str(row[4])),
        fetched_ts=int(str(row[5])),
        symbols=[str(s) for s in json.loads(str(row[6]))],
        sentiment=None if row[7] is None else float(str(row[7])),
        sentiment_method=None if row[8] is None else str(row[8]),
        confidence=None if row[9] is None else float(str(row[9])),
        importance=imp if imp in ("low", "medium", "high") else None,
    )


class NewsRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def exists(self, item: NewsItem) -> bool:
        """Дубликат по `content_hash` или по `url` (дедуп §8.2)."""
        row = self._conn.execute(
            "SELECT 1 FROM news WHERE content_hash = ? OR (? IS NOT NULL AND url = ?) LIMIT 1",
            (item.content_hash, item.url, item.url),
        ).fetchone()
        return row is not None

    def insert(self, item: NewsItem, score: SentimentScore, *, fetched_ts: int) -> str:
        """Новая строка `news`; возвращает ULID. Дубликат — `sqlite3.IntegrityError`."""
        news_id = new_ulid(fetched_ts)
        self._conn.execute(
            "INSERT INTO news(id, source, external_id, url, content_hash, title, summary, "
            "published_ts, fetched_ts, symbols_json, sentiment, sentiment_method, relevance, "
            "confidence, importance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                news_id,
                item.source,
                item.external_id,
                item.url,
                item.content_hash,
                item.title,
                item.summary,
                int(item.published_at.timestamp() * 1000),
                fetched_ts,
                json.dumps(item.symbols, ensure_ascii=False),
                score.sentiment,
                score.method,
                1.0 if item.symbols else 0.0,
                score.confidence,
                score.importance,
            ),
        )
        return news_id

    def last_published_ts(self, source: str) -> int | None:
        row = self._conn.execute(
            "SELECT MAX(published_ts) FROM news WHERE source = ?", (source,)
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def count_window(
        self,
        symbol: str,
        *,
        start_ts: int,
        end_ts: int,
        importance_in: Sequence[str] | None = None,
    ) -> int:
        """Новости по символу с `published_ts` в [start_ts, end_ts] (фильтр по importance)."""
        sql = (
            "SELECT COUNT(*) FROM news WHERE published_ts BETWEEN ? AND ? "
            "AND EXISTS (SELECT 1 FROM json_each(news.symbols_json) WHERE value = ?)"
        )
        params: list[object] = [start_ts, end_ts, symbol]
        if importance_in:
            sql += f" AND importance IN ({','.join('?' * len(importance_in))})"
            params.extend(importance_in)
        row = self._conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    def recent(self, *, symbol: str | None = None, limit: int = 20) -> list[NewsRecord]:
        """Последние новости (по `published_ts` убыв.), опционально по символу."""
        if symbol is None:
            rows = self._conn.execute(
                f"SELECT {_COLS} FROM news ORDER BY published_ts DESC, id DESC LIMIT ?",  # noqa: S608
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT {_COLS} FROM news WHERE EXISTS "  # noqa: S608 — константный список колонок
                "(SELECT 1 FROM json_each(news.symbols_json) WHERE value = ?) "
                "ORDER BY published_ts DESC, id DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        return [_record(r) for r in rows]
