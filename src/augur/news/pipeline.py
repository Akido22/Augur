"""`NewsPipeline` (ARCHITECTURE.md §8.2):

fetch (по источнику) → normalize → dedup (content_hash + url) → symbol mapping →
scoring (`SentimentScorer`) → persist(news) → `news_event` → reactions.

Каждая новость — отдельная транзакция: строка `news`, реакции и их аудит коммитятся вместе.
Подписчики `news_event` вызываются после COMMIT. Ошибка одного источника не останавливает
остальные (`NEWS_FETCH_FAILED` в аудите). Новостной контур не создаёт ордеров и сигналов.
"""

import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from augur.config.settings import Settings
from augur.db.audit import append_audit
from augur.ids import new_ulid
from augur.news.fetchers.base import NewsFetcher, NewsFetchError
from augur.news.models import NewsEvent, RawNewsItem
from augur.news.normalize import SymbolMapper, normalize
from augur.news.reactions import AppliedReaction, NewsReactionRules
from augur.news.repository import NewsRepository
from augur.news.scoring import RuleBasedScorer, SentimentScorer

__all__ = ["NewsPipeline", "NewsRunResult", "build_news_pipeline"]

log = logging.getLogger(__name__)
_CURSOR_OVERLAP = timedelta(minutes=10)  # запас на новости, опубликованные «задним числом»

EventHandler = Callable[[NewsEvent], None]


@dataclass
class NewsRunResult:
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    dropped: int = 0  # пустой заголовок после нормализации
    events: list[NewsEvent] = field(default_factory=list)
    reactions: list[AppliedReaction] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)  # source → текст ошибки


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class NewsPipeline:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        fetchers: Sequence[NewsFetcher],
        scorer: SentimentScorer,
        mapper: SymbolMapper,
        reactions: NewsReactionRules,
        lookback: timedelta = timedelta(hours=6),
        summary_max_chars: int = 1000,
        clock: Callable[[], int] | None = None,
        handlers: Sequence[EventHandler] = (),
    ) -> None:
        self._conn = conn
        self._fetchers = list(fetchers)
        self._scorer = scorer
        self._mapper = mapper
        self._reactions = reactions
        self._repo = NewsRepository(conn)
        self._lookback = lookback
        self._summary_max = summary_max_chars
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._handlers = list(handlers)

    @property
    def sources(self) -> list[str]:
        return [f.source for f in self._fetchers]

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def since(self, source: str, now_ms: int) -> datetime:
        """Курсор источника: последняя сохранённая новость − 10 мин, но не раньше lookback."""
        floor = datetime.fromtimestamp(now_ms / 1000, tz=UTC) - self._lookback
        last = self._repo.last_published_ts(source)
        if last is None:
            return floor
        return max(floor, datetime.fromtimestamp(last / 1000, tz=UTC) - _CURSOR_OVERLAP)

    async def run_once(self) -> NewsRunResult:
        """Один цикл по всем источникам. Возвращает статистику, события и реакции."""
        result = NewsRunResult()
        for fetcher in self._fetchers:
            now_ms = self._clock()
            try:
                raw = await fetcher.fetch(self.since(fetcher.source, now_ms))
            except NewsFetchError as exc:
                self._fetch_failed(fetcher.source, exc, now_ms)
                result.errors[fetcher.source] = str(exc)
                continue
            result.fetched += len(raw)
            for item in sorted(raw, key=lambda r: r.published_at):
                await self._process(item, result)
        return result

    async def _process(self, raw: RawNewsItem, result: NewsRunResult) -> None:
        item = normalize(raw, self._mapper, summary_max_chars=self._summary_max)
        if item is None:
            result.dropped += 1
            return
        if self._repo.exists(item):
            result.duplicates += 1
            return
        score = await self._scorer.score(item)
        now_ms = self._clock()
        try:
            news_id = self._repo.insert(item, score, fetched_ts=now_ms)
            event = NewsEvent(
                event_id=new_ulid(now_ms),
                news_id=news_id,
                symbols=item.symbols,
                published_at=item.published_at,
                sentiment=score.sentiment,
                confidence=score.confidence,
                sentiment_method=score.method,
                source=item.source,
                title=item.title,
                importance=score.importance,
            )
            applied = self._reactions.apply(event, now_ms=now_ms)
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()  # гонка дедупа: строку вставил другой процесс
            result.duplicates += 1
            return
        except BaseException:
            self._conn.rollback()
            raise
        result.inserted += 1
        result.events.append(event)
        result.reactions.extend(applied)
        for handler in self._handlers:
            try:
                handler(event)
            except Exception:  # noqa: BLE001 — подписчик не должен ронять пайплайн
                log.exception("news_event handler failed")

    def _fetch_failed(self, source: str, exc: NewsFetchError, now_ms: int) -> None:
        log.warning("news fetch failed [%s]: %s", source, exc)
        try:
            append_audit(
                self._conn,
                actor="news",
                event="NEWS_FETCH_FAILED",
                severity="WARN",
                ref_type="news",
                payload={"source": source, "error": str(exc), "retryable": exc.retryable},
                ts=now_ms,
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise


def build_news_pipeline(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    fetchers: Sequence[NewsFetcher] | None = None,
    clock: Callable[[], int] | None = None,
    scorer: SentimentScorer | None = None,
) -> NewsPipeline:
    """Composition root из настроек. Без `fetchers` — Finnhub (нужен `FINNHUB_API_KEY`).

    `scorer` — LLM-скорер Fusion (0.10.0) собирает вызывающий (interfaces): news не импортирует
    fusion. По умолчанию — `RuleBasedScorer`.

    Raises:
        ValueError: источник не настроен (нет ключа).
    """
    cfg = settings.news
    if fetchers is None:
        from augur.news.fetchers.finnhub import FinnhubFetcher

        if settings.finnhub_api_key is None:
            raise ValueError("FINNHUB_API_KEY is not set")
        fetchers = [
            FinnhubFetcher(
                settings.finnhub_api_key,
                categories=cfg.finnhub_categories,
                base_url=cfg.finnhub_base_url,
                timeout_s=cfg.timeout_s,
            )
        ]
    return NewsPipeline(
        conn,
        fetchers=fetchers,
        scorer=scorer if scorer is not None else RuleBasedScorer(cfg.source_weights),
        mapper=SymbolMapper(settings.symbols, cfg.symbol_aliases),
        reactions=NewsReactionRules(conn, cfg.rules),
        lookback=timedelta(hours=cfg.lookback_h),
        summary_max_chars=cfg.summary_max_chars,
        clock=clock,
    )
