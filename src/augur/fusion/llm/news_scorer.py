"""LLM-скорер новостей — реализация `SentimentScorer` в Fusion (VERSIONING §4, ARCH §8.2).

`name = "fusion:prompt_news_v1"`. Один вызов при temperature 0: оценка новости влияет только на
реакции, которые могут лишь ужесточить риск (пауза, мягкий флаг). Невалидный выход, бюджет,
сбой провайдера → результат `RuleBasedScorer` (`method = "rule:v1"`), без исключений.
"""

import logging
import sqlite3
from collections.abc import Callable, Mapping

from pydantic import SecretStr, ValidationError

from augur.config.settings import LLMSettings
from augur.fusion.llm.calls import CallLog, CallRunner
from augur.fusion.llm.client import LLMClient
from augur.fusion.llm.prompts import PromptTemplate, PromptVersionConflict
from augur.fusion.llm.sanitize import clean_text
from augur.fusion.llm.schema import NewsSentimentOut
from augur.ids import new_ulid
from augur.news.models import NewsItem, SentimentScore
from augur.news.scoring import RuleBasedScorer

__all__ = ["NEWS_PROMPT", "LLMNewsScorer"]

NEWS_PROMPT = "prompt_news_v1"
log = logging.getLogger(__name__)


class LLMNewsScorer:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: LLMSettings,
        client: LLMClient,
        keys: Mapping[str, SecretStr | None],
        *,
        fallback: RuleBasedScorer,
        clock: Callable[[], int],
        body_chars: int = 800,
    ) -> None:
        self._conn = conn
        self._cfg = cfg
        self._runner = CallRunner(conn, cfg, client, keys, clock=clock)
        self._prompt = PromptTemplate("news_sentiment", NEWS_PROMPT)
        self._fallback = fallback
        self._clock = clock
        self._body_chars = body_chars
        self.name = f"fusion:{NEWS_PROMPT}"

    async def score(self, item: NewsItem) -> SentimentScore:
        res = await self._score_llm(item)
        if res is None:
            return self._fallback.score_sync(item)
        return res

    async def _score_llm(self, item: NewsItem) -> SentimentScore | None:
        if self._cfg.mode != "enabled":
            return None
        try:
            self._prompt.ensure_registered(self._conn, self._cfg, 0.0, self._clock())
        except PromptVersionConflict as e:
            log.error("%s", e)
            return None
        if self._runner.breaker_open("news_sentiment"):
            return None
        group = new_ulid(self._clock())
        blocked = self._runner.budget_block(asset=None, event_driven=item_is_event(item))
        if blocked is not None:
            meta = CallLog("-", "news_sentiment", NEWS_PROMPT, None, group, 0, None)
            self._runner.log(meta, provider="-", model="-", status="budget_block", error=blocked)
            return None
        news_id = item.content_hash[:16]
        system, user = self._prompt.render(
            news_id=news_id,
            published_iso=item.published_at.isoformat(),
            source=clean_text(item.source, 40),
            symbols=",".join(item.symbols),
            title=clean_text(item.title, 300),
            body=clean_text(item.summary, self._body_chars),
        )
        asset = item.symbols[0] if len(item.symbols) == 1 else None
        meta = CallLog(
            self._prompt.prompt_hash(system, user),
            "news_sentiment",
            NEWS_PROMPT,
            asset,
            group,
            0,
            0.0,
        )
        att = await self._runner.call(meta, system=system, user=user)
        if att is None:
            return None
        try:
            out = NewsSentimentOut.model_validate_json(att.text, context={"news_id": news_id})
        except ValidationError as e:
            msg = str(e)[:500]
            self._runner.mark(
                att.call_id, "ungrounded" if "ungrounded" in msg else "invalid_schema", msg
            )
            return None
        return SentimentScore(
            sentiment=out.sentiment,
            confidence=out.confidence,
            importance=out.importance,
            method=self.name,
        )


def item_is_event(item: NewsItem) -> bool:
    """Новость сама по себе — событие: при 80 % бюджета её оценка ещё разрешена (§3.1)."""
    del item
    return True
