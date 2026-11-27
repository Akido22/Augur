"""Источники новостей (ARCHITECTURE.md §8.1). В 0.8.0 — только Finnhub."""

from augur.news.fetchers.base import NewsFetcher, NewsFetchError
from augur.news.fetchers.finnhub import FinnhubFetcher

__all__ = ["FinnhubFetcher", "NewsFetchError", "NewsFetcher"]
