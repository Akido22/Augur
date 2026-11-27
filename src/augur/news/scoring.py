"""Скоринг новостей (ARCHITECTURE.md §8.2): протокол `SentimentScorer` и `RuleBasedScorer`.

`RuleBasedScorer` ("rule:v1") — словарь позитивных и негативных терминов с весами и вес
источника; детерминирован, без сети. Любое изменение словаря или формул — новая версия
метода ("rule:v2"), иначе `sentiment_method` в `news` перестаёт однозначно описывать скор.

LLM-скорер — реализация `SentimentScorer` в Fusion-модуле (0.10.0, FUSION SPEC §3); здесь его
нет и не будет: ML/LLM-логика только в fusion (CLAUDE.md).
"""

import re
from collections.abc import Mapping
from typing import Protocol

from augur.news.models import Importance, NewsItem, SentimentScore

__all__ = ["RULE_V1", "RuleBasedScorer", "SentimentScorer"]

RULE_V1 = "rule:v1"

# Термин → вес (знак = направление). Совпадение — целое слово/фраза без учёта регистра;
# каждый термин считается один раз.
_STRONG_NEG = -3.0
_MILD_NEG = -1.5
_MILD_POS = 1.5
_STRONG_POS = 3.0
# Списки — через "|" (в фразах бывают пробелы).
_LEXICON: dict[str, float] = {
    **dict.fromkeys(
        "hack|hacked|exploit|exploited|bankruptcy|bankrupt|insolvent|insolvency|fraud|collapse|"
        "collapses|crash|crashes|plunge|plunges|ban|bans|banned|lawsuit|sued|halt|halted|halts|"
        "delisting|delisted|depeg|depegged|stolen|liquidations".split("|"),
        _STRONG_NEG,
    ),
    **dict.fromkeys(
        "falls|drop|drops|declines|slump|selloff|sell-off|bearish|outflows|losses|warning|"
        "investigation|probe|fined|downgrade".split("|"),
        _MILD_NEG,
    ),
    **dict.fromkeys(
        "gains|rises|bullish|inflows|adoption|partnership|upgrade|launches|beats".split("|"),
        _MILD_POS,
    ),
    **dict.fromkeys(
        "surge|surges|soars|rally|rallies|record high|all-time high|approval|approved".split("|"),
        _STRONG_POS,
    ),
}
# Термины, делающие новость high / medium importance (независимо от знака).
_HIGH_IMPACT = (
    "hack|hacked|exploit|exploited|bankruptcy|bankrupt|insolvent|fraud|ban|banned|halt|halted|"
    "delisting|delisted|depeg|depegged|stolen|sec|cftc|lawsuit|sued|etf|fomc|cpi|rate hike|"
    "rate cut|federal reserve"
).split("|")
_MEDIUM_IMPACT = (
    "regulation|regulator|regulators|investigation|probe|liquidations|outflows|inflows|earnings|"
    "upgrade|downgrade"
).split("|")
_SCALE = 1.5  # sentiment = raw / (|raw| + _SCALE): один сильный термин → ±0.67
_NO_HITS_CONFIDENCE = 0.1


def _pattern(terms: list[str]) -> re.Pattern[str]:
    alt = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<![\w-])(?:{alt})(?![\w-])", re.IGNORECASE)


_LEX_RE = _pattern(list(_LEXICON))
_HIGH_RE = _pattern(_HIGH_IMPACT)
_MEDIUM_RE = _pattern(_MEDIUM_IMPACT)


class SentimentScorer(Protocol):
    """Скорер новости. `name` пишется в `news.sentiment_method` / `news_event.sentiment_method`."""

    name: str

    async def score(self, item: NewsItem) -> SentimentScore: ...


class RuleBasedScorer:
    """Детерминированный словарный скорер `rule:v1`.

    sentiment = raw / (|raw| + 1.5), raw — сумма весов найденных терминов;
    confidence = min(1, 0.3 + 0.2·hits) · (0.5 + 0.5·согласие) · вес источника, где
    согласие = |raw| / Σ|w| (1 — все термины одного знака, 0 — полностью смешанные);
    без терминов — sentiment 0, confidence 0.1. importance: high-impact термин → high,
    medium-термин → medium, иначе low.
    """

    name = RULE_V1

    def __init__(self, source_weights: Mapping[str, float] | None = None) -> None:
        self._weights = dict(source_weights or {})

    def score_sync(self, item: NewsItem) -> SentimentScore:
        text = f"{item.title} {item.summary or ''}"
        hits = {m.group(0).lower() for m in _LEX_RE.finditer(text)}
        weights = [_LEXICON[h] for h in hits]
        importance: Importance = (
            "high" if _HIGH_RE.search(text) else "medium" if _MEDIUM_RE.search(text) else "low"
        )
        if not weights:
            return SentimentScore(
                sentiment=0.0,
                confidence=_NO_HITS_CONFIDENCE,
                importance=importance,
                method=self.name,
            )
        raw = sum(weights)
        agreement = abs(raw) / sum(abs(w) for w in weights)
        src = max(0.0, self._weights.get(item.source, 1.0))
        confidence = min(1.0, 0.3 + 0.2 * len(weights)) * (0.5 + 0.5 * agreement) * src
        return SentimentScore(
            sentiment=round(raw / (abs(raw) + _SCALE), 6),
            confidence=round(min(1.0, confidence), 6),
            importance=importance,
            method=self.name,
        )

    async def score(self, item: NewsItem) -> SentimentScore:
        return self.score_sync(item)
