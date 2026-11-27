"""Протокол источника новостей (ARCHITECTURE.md §8.2)."""

from datetime import datetime
from typing import Protocol

from augur.news.models import RawNewsItem

__all__ = ["NewsFetchError", "NewsFetcher"]


class NewsFetchError(Exception):
    """Источник недоступен или ответил ошибкой. Текст не содержит ключей и токенов.

    Attributes:
        retryable: временная ошибка (таймаут, 429, 5xx) — повторить на следующем цикле.
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class NewsFetcher(Protocol):
    """Источник: возвращает новости, опубликованные не раньше `since` (aware UTC)."""

    source: str

    async def fetch(self, since: datetime) -> list[RawNewsItem]: ...
