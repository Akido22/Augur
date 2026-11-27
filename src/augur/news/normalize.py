"""Нормализация, `content_hash` и привязка к символам (ARCHITECTURE.md §8.2).

Текст новостей — недоверенные данные: управляющие символы вырезаются, пробелы схлопываются,
длина обрезается. Детерминированно, без сети и LLM.
"""

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from augur.news.models import NewsItem, RawNewsItem

__all__ = ["SymbolMapper", "clean_text", "content_hash", "normalize", "source_domain"]

_WS = re.compile(r"\s+")
_TITLE_MAX = 300  # news_event.title maxLength (§8.3)


def clean_text(text: str | None, max_chars: int) -> str:
    """NFKC, без управляющих символов, схлопнутые пробелы, обрезка до `max_chars`."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in text)
    text = _WS.sub(" ", text).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def source_domain(raw: RawNewsItem) -> str:
    """Издатель для хэша: `source_domain` источника, иначе хост URL, иначе имя источника."""
    if raw.source_domain:
        return raw.source_domain.strip().lower()
    if raw.url:
        host = urlsplit(raw.url).hostname
        if host:
            return host.lower().removeprefix("www.")
    return raw.source.lower()


def content_hash(title: str, domain: str) -> str:
    """sha256(нормализованный заголовок + домен источника) — ключ дедупа (§3.2)."""
    norm = _WS.sub(" ", re.sub(r"[^\w\s]", " ", title.casefold())).strip()
    return hashlib.sha256(f"{norm}|{domain}".encode()).hexdigest()


class SymbolMapper:
    """Привязка новости к `settings.symbols`.

    Символ подходит, если базовый актив (`BTC/USDT` → `BTC`) или сам символ есть в `related`
    источника, либо базовый актив / алиас встречается в заголовке или summary как целое слово
    (без учёта регистра).
    """

    def __init__(self, symbols: Sequence[str], aliases: Mapping[str, Sequence[str]]) -> None:
        self._symbols = list(symbols)
        self._tickers: dict[str, set[str]] = {}
        self._patterns: dict[str, re.Pattern[str]] = {}
        for sym in self._symbols:
            base = sym.split("/")[0].split(":")[0].strip()
            self._tickers[sym] = {sym.upper(), base.upper()}
            words = sorted({base, *aliases.get(sym, ())}, key=len, reverse=True)
            alt = "|".join(re.escape(w) for w in words if w)
            self._patterns[sym] = re.compile(rf"(?<!\w)(?:{alt})(?!\w)", re.IGNORECASE)

    def map(self, title: str, summary: str, related: Sequence[str]) -> list[str]:
        rel = {r.strip().upper() for r in related if r.strip()}
        text = f"{title} {summary}"
        return [
            s
            for s in self._symbols
            if self._tickers[s] & rel or self._patterns[s].search(text) is not None
        ]


def normalize(raw: RawNewsItem, mapper: SymbolMapper, *, summary_max_chars: int) -> NewsItem | None:
    """Сырая новость → `NewsItem`. Пустой заголовок после очистки → None (новость отброшена)."""
    title = clean_text(raw.title, _TITLE_MAX)
    if not title:
        return None
    summary = clean_text(raw.summary, summary_max_chars)
    url = raw.url.strip() if raw.url and raw.url.strip() else None
    return NewsItem(
        source=raw.source,
        external_id=raw.external_id,
        url=url,
        content_hash=content_hash(title, source_domain(raw)),
        title=title,
        summary=summary or None,
        published_at=raw.published_at,
        symbols=mapper.map(title, summary, raw.related),
    )
