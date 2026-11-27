"""Новостной контур (ARCHITECTURE.md §8, Augur 0.8.0): Finnhub → нормализация → дедуп →
`RuleBasedScorer` → `news` → `news_event` → `NewsReactionRules`.

Без LLM: LLM-скорер — реализация `SentimentScorer` в fusion-модуле (0.10.0).
Контур не создаёт ордеров и сигналов на вход; реакции только ужесточают риск (R15/R16).
"""

from augur.news.fetchers import FinnhubFetcher, NewsFetcher, NewsFetchError
from augur.news.models import NewsEvent, NewsItem, RawNewsItem, SentimentScore
from augur.news.normalize import SymbolMapper, content_hash, normalize
from augur.news.pipeline import NewsPipeline, NewsRunResult, build_news_pipeline
from augur.news.reactions import (
    AppliedReaction,
    NewsReactionRules,
    ReactionNotFoundError,
    ReactionRecord,
)
from augur.news.repository import NewsRecord, NewsRepository
from augur.news.scoring import RULE_V1, RuleBasedScorer, SentimentScorer

__all__ = [
    "RULE_V1",
    "AppliedReaction",
    "FinnhubFetcher",
    "NewsEvent",
    "NewsFetchError",
    "NewsFetcher",
    "NewsItem",
    "NewsPipeline",
    "NewsReactionRules",
    "NewsRecord",
    "NewsRepository",
    "NewsRunResult",
    "RawNewsItem",
    "ReactionNotFoundError",
    "ReactionRecord",
    "RuleBasedScorer",
    "SentimentScorer",
    "SentimentScore",
    "SymbolMapper",
    "build_news_pipeline",
    "content_hash",
    "normalize",
]
