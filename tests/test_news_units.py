"""Новостной контур 0.8.0: нормализация, хэш, привязка к символам, RuleBasedScorer, Finnhub
(мок httpx), конфиг правил, миграция 0006_news."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from augur.config.settings import NewsFlag, NewsRule, NewsSettings
from augur.news import (
    RULE_V1,
    FinnhubFetcher,
    NewsFetchError,
    NewsItem,
    RawNewsItem,
    RuleBasedScorer,
    SymbolMapper,
    content_hash,
    normalize,
)
from augur.news.normalize import clean_text

T0 = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
MAPPER = SymbolMapper(["BTC/USDT", "ETH/USDT"], {"BTC/USDT": ["bitcoin"], "ETH/USDT": ["ether"]})


def raw(title: str, **over: object) -> RawNewsItem:
    data: dict[str, object] = {
        "source": "finnhub",
        "title": title,
        "published_at": T0,
        "url": "https://www.coindesk.com/a",
    }
    data.update(over)
    return RawNewsItem(**data)  # type: ignore[arg-type]


def item(title: str, summary: str | None = None, source: str = "finnhub") -> NewsItem:
    return NewsItem(
        source=source,
        content_hash="0" * 64,
        title=title,
        summary=summary,
        published_at=T0,
        symbols=["BTC/USDT"],
    )


# --- нормализация ---------------------------------------------------------------------


def test_clean_text_strips_control_chars_and_truncates() -> None:
    assert clean_text("  Bitcoin\x00\x1b[31m  rallies\n\tnow ", 100) == "Bitcoin [31m rallies now"
    assert clean_text("x" * 20, 10) == "x" * 9 + "…"
    assert clean_text(None, 10) == ""


def test_content_hash_ignores_case_punctuation_and_spacing() -> None:
    a = content_hash("Bitcoin  Rallies!", "coindesk.com")
    assert a == content_hash("bitcoin rallies", "coindesk.com")
    assert a != content_hash("bitcoin rallies", "reuters.com")
    assert len(a) == 64


def test_normalize_uses_publisher_domain_for_hash() -> None:
    by_url = normalize(raw("Bitcoin rallies"), MAPPER, summary_max_chars=50)
    by_src = normalize(
        raw("Bitcoin rallies", source_domain="CoinDesk"), MAPPER, summary_max_chars=50
    )
    assert by_url is not None and by_src is not None
    assert by_url.content_hash == content_hash("Bitcoin rallies", "coindesk.com")
    assert by_src.content_hash == content_hash("Bitcoin rallies", "coindesk")


def test_normalize_drops_empty_title_and_truncates_summary() -> None:
    assert normalize(raw("\x00 \n"), MAPPER, summary_max_chars=50) is None
    n = normalize(raw("Bitcoin rallies", summary="y" * 100, url="  "), MAPPER, summary_max_chars=10)
    assert n is not None and n.summary == "y" * 9 + "…" and n.url is None


@pytest.mark.parametrize(
    ("title", "related", "expected"),
    [
        ("Bitcoin hits a new high", [], ["BTC/USDT"]),
        ("BTC and ETH slide", [], ["BTC/USDT", "ETH/USDT"]),
        ("Ether staking grows", [], ["ETH/USDT"]),
        ("Market wrap", ["ETH"], ["ETH/USDT"]),
        ("Market wrap", ["BTC/USDT"], ["BTC/USDT"]),
        ("Bitcoiners meet; BTCX launches", [], []),  # только целые слова
        ("Fed holds rates", [], []),
    ],
)
def test_symbol_mapping(title: str, related: list[str], expected: list[str]) -> None:
    assert MAPPER.map(title, "", related) == expected


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        raw("x", published_at=datetime(2026, 1, 1))  # noqa: DTZ001


# --- RuleBasedScorer ------------------------------------------------------------------


def test_scorer_neutral_without_terms() -> None:
    s = RuleBasedScorer().score_sync(item("Bitcoin conference starts in Lisbon"))
    assert (s.sentiment, s.confidence, s.importance, s.method) == (0.0, 0.1, "low", RULE_V1)


def test_scorer_strong_negative_is_high_importance() -> None:
    s = RuleBasedScorer().score_sync(item("Exchange hacked, funds stolen", "Withdrawals halted"))
    assert s.sentiment < -0.6 and s.confidence >= 0.6 and s.importance == "high"


def test_scorer_positive_and_mixed() -> None:
    pos = RuleBasedScorer().score_sync(item("Bitcoin rally: price soars to record high"))
    assert pos.sentiment > 0.6 and pos.importance == "low"
    mixed = RuleBasedScorer().score_sync(item("Bitcoin surges despite SEC lawsuit"))
    assert abs(mixed.sentiment) < 0.2 and mixed.importance == "high"
    assert mixed.confidence < pos.confidence  # смешанные термины снижают уверенность


def test_scorer_is_deterministic_and_source_weighted() -> None:
    it = item("Bitcoin drops after regulator warning")
    a, b = RuleBasedScorer().score_sync(it), RuleBasedScorer().score_sync(it)
    assert a == b and a.importance == "medium"
    half = RuleBasedScorer({"finnhub": 0.5}).score_sync(it)
    assert half.sentiment == a.sentiment and half.confidence == pytest.approx(a.confidence / 2)


def test_scorer_word_boundaries() -> None:
    s = RuleBasedScorer().score_sync(item("Hackathon winners announced", "Banners everywhere"))
    assert s.sentiment == 0.0 and s.importance == "low"


async def test_scorer_async_protocol() -> None:
    s = await RuleBasedScorer().score(item("Bitcoin gains"))
    assert s.sentiment > 0


# --- Finnhub --------------------------------------------------------------------------

KEY = "finnhub-test-key-123"


def _row(i: int, ts: datetime, **over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "category": "crypto",
        "datetime": int(ts.timestamp()),
        "headline": f"Bitcoin news {i}",
        "id": 1000 + i,
        "image": "",
        "related": "",
        "source": "CoinDesk",
        "summary": "summary",
        "url": f"https://example.com/{i}",
    }
    row.update(over)
    return row


def _fetcher(handler: httpx.MockTransport, categories: list[str] | None = None) -> FinnhubFetcher:
    client = httpx.AsyncClient(transport=handler)
    return FinnhubFetcher(
        SecretStr(KEY),
        categories=categories or ["crypto"],
        base_url="https://finnhub.test/api/v1",
        client=client,
    )


async def test_finnhub_fetch_parses_and_filters_since() -> None:
    seen: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200,
            json=[
                _row(1, T0, related="BTC,COIN"),
                _row(2, T0 - timedelta(hours=7)),  # раньше since
                {"headline": "broken"},  # без datetime — пропускается
                "garbage",
            ],
        )

    items = await _fetcher(httpx.MockTransport(handle)).fetch(T0 - timedelta(hours=6))
    assert [i.external_id for i in items] == ["1001"]
    it = items[0]
    assert it.source == "finnhub" and it.related == ["BTC", "COIN"]
    assert it.source_domain == "CoinDesk" and it.published_at == T0
    req = seen[0]
    assert req.headers["X-Finnhub-Token"] == KEY
    assert KEY not in str(req.url)  # ключ только в заголовке
    assert req.url.params["category"] == "crypto"


async def test_finnhub_multiple_categories() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        cat = req.url.params["category"]
        return httpx.Response(200, json=[_row(1 if cat == "crypto" else 2, T0)])

    items = await _fetcher(httpx.MockTransport(handle), ["crypto", "general"]).fetch(T0)
    assert sorted(i.external_id or "" for i in items) == ["1001", "1002"]


@pytest.mark.parametrize(
    ("status", "retryable"), [(401, False), (403, False), (429, True), (500, True), (503, True)]
)
async def test_finnhub_http_errors(status: int, retryable: bool) -> None:
    fetcher = _fetcher(httpx.MockTransport(lambda r: httpx.Response(status, text="no")))
    with pytest.raises(NewsFetchError) as ei:
        await fetcher.fetch(T0)
    assert ei.value.retryable is retryable
    assert KEY not in str(ei.value)


async def test_finnhub_timeout_and_bad_payload() -> None:
    def timeout(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(NewsFetchError, match="timeout") as ei:
        await _fetcher(httpx.MockTransport(timeout)).fetch(T0)
    assert ei.value.retryable
    obj = _fetcher(httpx.MockTransport(lambda r: httpx.Response(200, json={"error": "x"})))
    with pytest.raises(NewsFetchError, match="unexpected payload"):
        await obj.fetch(T0)
    bad = _fetcher(httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")))
    with pytest.raises(NewsFetchError, match="invalid JSON"):
        await bad.fetch(T0)


def test_finnhub_requires_key() -> None:
    with pytest.raises(ValueError, match="empty"):
        FinnhubFetcher(SecretStr(""), categories=["crypto"])


def test_finnhub_key_not_in_repr() -> None:
    f = FinnhubFetcher(SecretStr(KEY), categories=["crypto"])
    assert KEY not in repr(vars(f))


# --- конфиг правил --------------------------------------------------------------------


def test_default_rules_match_arch() -> None:
    rules = {r.name: r for r in NewsSettings().rules}
    assert set(rules) == {"strong_negative_pause", "mixed_reduce_risk", "any_high_alert"}
    assert rules["strong_negative_pause"].action == "pause_symbol"
    assert rules["mixed_reduce_risk"].flag == NewsFlag(code="news_mixed", severity="medium")


def test_rule_validation() -> None:
    with pytest.raises(ValidationError, match="flag is required"):
        NewsRule(name="x", action="add_risk_flag")
    with pytest.raises(ValidationError, match="strategies are required"):
        NewsRule(name="x", action="pause_strategy")
    with pytest.raises(ValidationError, match="flag is required"):
        NewsRule(name="x", action="pause_symbol", flag=NewsFlag(code="a", severity="low"))
    with pytest.raises(ValidationError):
        NewsRule(name="x", action="reduce_risk")  # type: ignore[arg-type]  # не в 0.8.0


def test_rules_from_env_json(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import make_settings

    monkeypatch.setenv(
        "NEWS__RULES",
        json.dumps([{"name": "halt", "when": {"sentiment_lt": -0.9}, "action": "pause_symbol"}]),
    )
    s = make_settings()
    assert [r.name for r in s.news.rules] == ["halt"]


# --- миграция 0006 --------------------------------------------------------------------


def _news(db: sqlite3.Connection, nid: str, h: str, url: str | None) -> None:
    db.execute(
        "INSERT INTO news(id, source, url, content_hash, title, published_ts, fetched_ts) "
        "VALUES (?, 'finnhub', ?, ?, 't', 1, 1)",
        (nid, url, h),
    )


def test_migration_0006_dedup_indexes(db: sqlite3.Connection) -> None:
    _news(db, "n1", "h1", "https://a")
    _news(db, "n2", "h2", None)
    _news(db, "n3", "h3", None)  # несколько NULL url допустимы
    with pytest.raises(sqlite3.IntegrityError):
        _news(db, "n4", "h1", "https://b")
    with pytest.raises(sqlite3.IntegrityError):
        _news(db, "n5", "h5", "https://a")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO news(id, source, content_hash, title, published_ts, fetched_ts, "
            "sentiment) VALUES ('n6', 's', 'h6', 't', 1, 1, 1.5)"
        )


def test_migration_0006_reaction_checks(db: sqlite3.Connection) -> None:
    _news(db, "n1", "h1", None)
    ok = (
        "INSERT INTO news_reactions(id, rule, action, symbol, strategy, flag_code, "
        "flag_severity, news_id, created_ts, expires_ts) VALUES (?, 'r', ?, ?, ?, ?, ?, 'n1', 1, ?)"
    )
    db.execute(ok, ("a", "pause_symbol", "BTC/USDT", None, None, None, 2))
    db.execute(ok, ("b", "pause_strategy", None, "sma_cross", None, None, 2))
    db.execute(ok, ("c", "add_risk_flag", "BTC/USDT", None, "news_mixed", "medium", 2))
    bad = [
        ("d", "pause_symbol", None, None, None, None, 2),  # без символа
        ("e", "pause_strategy", None, None, None, None, 2),  # без стратегии
        ("f", "add_risk_flag", "BTC/USDT", None, None, None, 2),  # без флага
        ("g", "pause_symbol", "BTC/USDT", None, None, None, 1),  # expires <= created
        ("h", "notify", "BTC/USDT", None, None, None, 2),  # notify не хранится
    ]
    for row in bad:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(ok, row)
