"""Finnhub: market news по категориям (ARCHITECTURE.md §8.1).

`GET {base}/news?category=<crypto|general|forex|merger>`. Ключ передаётся только заголовком
`X-Finnhub-Token`, не в URL: URL попадает в логи и тексты исключений httpx.
Фильтр по `since` — на клиенте (эндпоинт отдаёт последние новости категории).
"""

import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import SecretStr

from augur.news.fetchers.base import NewsFetchError
from augur.news.models import RawNewsItem

__all__ = ["FinnhubFetcher"]

log = logging.getLogger(__name__)


class FinnhubFetcher:
    """Источник `finnhub`.

    Args:
        api_key: `FINNHUB_API_KEY` из `.env`.
        categories: категории market news.
        base_url: базовый URL API.
        timeout_s: таймаут одного запроса.
        client: внешний `httpx.AsyncClient` (тесты: `MockTransport`); иначе создаётся на вызов.
    """

    source = "finnhub"

    def __init__(
        self,
        api_key: SecretStr,
        *,
        categories: list[str],
        base_url: str = "https://finnhub.io/api/v1",
        timeout_s: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.get_secret_value():
            raise ValueError("FINNHUB_API_KEY is empty")
        self._key = api_key
        self._categories = list(categories)
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        self._client = client

    async def fetch(self, since: datetime) -> list[RawNewsItem]:
        if self._client is not None:
            return await self._fetch_all(self._client, since)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._fetch_all(client, since)

    async def _fetch_all(self, client: httpx.AsyncClient, since: datetime) -> list[RawNewsItem]:
        out: list[RawNewsItem] = []
        for category in self._categories:
            for row in await self._get(client, category):
                item = self._parse(row)
                if item is not None and item.published_at >= since:
                    out.append(item)
        return out

    async def _get(self, client: httpx.AsyncClient, category: str) -> list[Any]:
        try:
            resp = await client.get(
                f"{self._base}/news",
                params={"category": category},
                headers={"X-Finnhub-Token": self._key.get_secret_value()},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise NewsFetchError(f"finnhub timeout ({category})", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise NewsFetchError(
                f"finnhub transport error ({category}): {type(exc).__name__}", retryable=True
            ) from exc
        if resp.status_code != 200:
            retryable = resp.status_code == 429 or resp.status_code >= 500
            raise NewsFetchError(
                f"finnhub HTTP {resp.status_code} ({category})", retryable=retryable
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise NewsFetchError(f"finnhub invalid JSON ({category})", retryable=True) from exc
        if not isinstance(data, list):
            raise NewsFetchError(f"finnhub unexpected payload ({category})", retryable=False)
        return data

    def _parse(self, row: Any) -> RawNewsItem | None:
        """Строка ответа → `RawNewsItem`; битая строка пропускается (с предупреждением)."""
        try:
            if not isinstance(row, dict):
                raise TypeError("row is not an object")
            related = str(row.get("related") or "")
            ext = row.get("id")
            return RawNewsItem(
                source=self.source,
                external_id=str(ext) if ext is not None else None,
                url=str(row["url"]) if row.get("url") else None,
                title=str(row.get("headline") or ""),
                summary=str(row["summary"]) if row.get("summary") else None,
                published_at=datetime.fromtimestamp(int(row["datetime"]), tz=UTC),
                related=[t for t in related.replace(";", ",").split(",") if t.strip()],
                source_domain=str(row["source"]) if row.get("source") else None,
            )
        except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
            log.warning("finnhub: skipped malformed item: %s", type(exc).__name__)
            return None
