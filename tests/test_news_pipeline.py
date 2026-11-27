"""NewsPipeline, NewsReactionRules, связка с Risk Gate (R15/R16/soft) и CLI `augur news`."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from augur.config.settings import NewsRule, NewsRuleWhen, RiskLimits
from augur.db.migrator import Migrator
from augur.fusion.contract import RiskFlag
from augur.interfaces.cli.main import app
from augur.news import (
    NewsEvent,
    NewsFetchError,
    NewsPipeline,
    NewsReactionRules,
    NewsRepository,
    RawNewsItem,
    ReactionNotFoundError,
    build_news_pipeline,
)
from augur.risk.context import MarketSnapshot, RiskContextBuilder
from augur.risk.gate import RiskGate
from augur.strategies.base import SignalAction
from tests._risk import INSTRUMENT, NOW, make_ctx, make_signal
from tests.conftest import make_settings

NOW_MS = int(NOW.timestamp() * 1000)
MIN = 60_000


class FakeFetcher:
    def __init__(self, items: list[RawNewsItem], source: str = "finnhub") -> None:
        self.source = source
        self.items = items
        self.since: list[datetime] = []
        self.error: NewsFetchError | None = None

    async def fetch(self, since: datetime) -> list[RawNewsItem]:
        self.since.append(since)
        if self.error is not None:
            raise self.error
        return [i for i in self.items if i.published_at >= since]


def raw(title: str, *, minutes_ago: int = 5, url: str | None = None, **over: object) -> RawNewsItem:
    data: dict[str, object] = {
        "source": "finnhub",
        "title": title,
        "published_at": NOW - timedelta(minutes=minutes_ago),
        "url": url,
        "source_domain": "coindesk",
    }
    data.update(over)
    return RawNewsItem(**data)  # type: ignore[arg-type]


HACK = "Bitcoin exchange hacked, funds stolen"  # sentiment < -0.6, conf ≥ 0.6, high


def pipeline(
    db: sqlite3.Connection, fetchers: list[FakeFetcher], **settings: object
) -> NewsPipeline:
    return build_news_pipeline(
        db, make_settings(**settings), fetchers=fetchers, clock=lambda: NOW_MS
    )


def audit_events(db: sqlite3.Connection) -> list[tuple[str, dict[str, object]]]:
    rows = db.execute("SELECT event, payload_json FROM audit_log ORDER BY id").fetchall()
    return [(str(e), json.loads(p)) for e, p in rows]


# --- пайплайн -------------------------------------------------------------------------


async def test_run_once_persists_scores_and_emits_events(db: sqlite3.Connection) -> None:
    f = FakeFetcher([raw(HACK, url="https://x/1"), raw("Ether staking grows", minutes_ago=20)])
    p = pipeline(db, [f])
    seen: list[NewsEvent] = []
    p.subscribe(seen.append)
    res = await p.run_once()
    assert (res.fetched, res.inserted, res.duplicates, res.dropped) == (2, 2, 0, 0)
    assert [e.title for e in res.events] == ["Ether staking grows", HACK]  # по времени
    assert seen == res.events
    hack = res.events[1]
    assert hack.symbols == ["BTC/USDT"] and hack.importance == "high"
    assert hack.sentiment_method == "rule:v1" and hack.sentiment < -0.6
    rows = NewsRepository(db).recent()
    assert {r.title for r in rows} == {HACK, "Ether staking grows"}
    assert rows[0].sentiment_method == "rule:v1"
    # первый fetch — окно lookback (6 ч)
    assert f.since == [NOW - timedelta(hours=6)]


async def test_dedup_on_rerun_and_within_batch(db: sqlite3.Connection) -> None:
    f = FakeFetcher(
        [
            raw("Bitcoin gains", url="https://x/1"),
            raw("BITCOIN  gains!", url="https://x/2", minutes_ago=4),  # тот же content_hash
            raw("Other title", url="https://x/1", minutes_ago=3),  # тот же url
        ]
    )
    p = pipeline(db, [f])
    first = await p.run_once()
    assert (first.inserted, first.duplicates) == (1, 2)
    second = await p.run_once()
    assert (second.inserted, second.duplicates) == (0, 3)  # все в окне курсора, все дубли
    assert db.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 1


async def test_cursor_uses_last_published_minus_overlap(db: sqlite3.Connection) -> None:
    f = FakeFetcher([raw("Bitcoin gains", minutes_ago=30)])
    p = pipeline(db, [f])
    await p.run_once()
    await p.run_once()
    assert f.since[1] == NOW - timedelta(minutes=40)


async def test_fetch_error_is_audited_and_other_sources_continue(db: sqlite3.Connection) -> None:
    bad = FakeFetcher([], source="broken")
    bad.error = NewsFetchError("broken HTTP 503 (crypto)", retryable=True)
    good = FakeFetcher([raw("Bitcoin gains")])
    res = await pipeline(db, [bad, good]).run_once()
    assert res.errors == {"broken": "broken HTTP 503 (crypto)"} and res.inserted == 1
    ev = [p for e, p in audit_events(db) if e == "NEWS_FETCH_FAILED"]
    assert ev == [{"source": "broken", "error": "broken HTTP 503 (crypto)", "retryable": True}]


async def test_handler_failure_does_not_break_pipeline(db: sqlite3.Connection) -> None:
    p = pipeline(db, [FakeFetcher([raw("Bitcoin gains"), raw("Ether gains", minutes_ago=6)])])

    def boom(_e: NewsEvent) -> None:
        raise RuntimeError("subscriber down")

    p.subscribe(boom)
    assert (await p.run_once()).inserted == 2


def test_build_requires_finnhub_key(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="FINNHUB_API_KEY"):
        build_news_pipeline(db, make_settings())
    p = build_news_pipeline(db, make_settings(finnhub_api_key="k"))
    assert p.sources == ["finnhub"]


# --- реакции --------------------------------------------------------------------------


async def test_strong_negative_pauses_symbol_with_audit(db: sqlite3.Connection) -> None:
    res = await pipeline(db, [FakeFetcher([raw(HACK, minutes_ago=5)])]).run_once()
    actions = sorted((r.rule, r.action, r.symbol) for r in res.reactions)
    assert actions == [
        ("any_high_alert", "notify", "BTC/USDT"),
        ("strong_negative_pause", "pause_symbol", "BTC/USDT"),
    ]
    active = NewsReactionRules(db, []).active(now_ms=NOW_MS)
    assert [(a.action, a.symbol) for a in active] == [("pause_symbol", "BTC/USDT")]
    assert active[0].expires_ts == NOW_MS - 5 * MIN + 60 * MIN  # published + duration
    reactions = [p for e, p in audit_events(db) if e == "NEWS_REACTION"]
    assert {p["action"] for p in reactions} == {"notify", "pause_symbol"}


async def test_old_news_does_not_pause_after_duration(db: sqlite3.Connection) -> None:
    res = await pipeline(db, [FakeFetcher([raw(HACK, minutes_ago=90)])]).run_once()
    assert [r.action for r in res.reactions] == ["notify"]
    assert NewsReactionRules(db, []).active(now_ms=NOW_MS) == []


async def test_mixed_window_adds_soft_flag(db: sqlite3.Connection) -> None:
    items = [
        raw("Bitcoin surges despite SEC lawsuit", minutes_ago=30),
        raw("Bitcoin ETF gains offset by CFTC probe", minutes_ago=20),
        raw("Bitcoin rally meets SEC lawsuit news", minutes_ago=10),
    ]
    res = await pipeline(db, [FakeFetcher(items)]).run_once()
    flags = [r for r in res.reactions if r.action == "add_risk_flag"]
    assert len(flags) == 1 and flags[0].symbol == "BTC/USDT"  # только с 3-й новостью в окне
    active = NewsReactionRules(db, []).active(now_ms=NOW_MS)
    assert [(a.flag_code, a.flag_severity) for a in active] == [("news_mixed", "medium")]


async def test_no_duplicate_active_reaction(db: sqlite3.Connection) -> None:
    items = [
        raw(HACK, minutes_ago=10),
        raw("Bitcoin exchange exploited, coins stolen", minutes_ago=10),  # тот же срок — не пишется
        raw("Bitcoin lender bankrupt, withdrawals halted", minutes_ago=50),
    ]
    await pipeline(db, [FakeFetcher(items)]).run_once()
    active = NewsReactionRules(db, []).active(now_ms=NOW_MS)
    # по времени публикации: более свежая новость продлевает паузу новой строкой
    assert sorted(a.expires_ts for a in active) == [NOW_MS + 10 * MIN, NOW_MS + 50 * MIN]


async def test_pause_strategy_and_news_without_symbols(db: sqlite3.Connection) -> None:
    rules = [
        NewsRule(
            name="halt_trend",
            when=NewsRuleWhen(importance_in=["high"]),
            action="pause_strategy",
            strategies=["sma_cross"],
        )
    ]
    r = NewsReactionRules(db, rules)
    db.execute(
        "INSERT INTO news(id, source, content_hash, title, published_ts, fetched_ts) "
        "VALUES ('n1', 'finnhub', 'h', 't', ?, ?)",
        (NOW_MS, NOW_MS),
    )

    def ev(symbols: list[str]) -> NewsEvent:
        return NewsEvent(
            event_id="e",
            news_id="n1",
            symbols=symbols,
            published_at=NOW,
            sentiment=-0.9,
            confidence=0.9,
            sentiment_method="rule:v1",
            source="finnhub",
            importance="high",
        )

    assert r.apply(ev([]), now_ms=NOW_MS) == []  # без символов — только notify
    applied = r.apply(ev(["BTC/USDT", "ETH/USDT"]), now_ms=NOW_MS)
    assert [(a.action, a.strategy, a.symbol) for a in applied] == [
        ("pause_strategy", "sma_cross", None)
    ]


async def test_lift_reaction(db: sqlite3.Connection) -> None:
    await pipeline(db, [FakeFetcher([raw(HACK)])]).run_once()
    rules = NewsReactionRules(db, [])
    rid = rules.active(now_ms=NOW_MS)[0].id
    rec = rules.lift(rid, actor="cli:test", now_ms=NOW_MS + 1)
    assert rec.symbol == "BTC/USDT" and rules.active(now_ms=NOW_MS + 2) == []
    lifted = [p for e, p in audit_events(db) if e == "NEWS_REACTION_LIFTED"]
    assert lifted[0]["reaction_id"] == rid
    with pytest.raises(ReactionNotFoundError):
        rules.lift(rid, actor="cli:test", now_ms=NOW_MS + 3)


# --- Risk Gate ------------------------------------------------------------------------


def _market() -> MarketSnapshot:
    c = make_ctx()
    return MarketSnapshot(
        market_status=c.market_status,
        price=c.price,
        spread_bps=c.spread_bps,
        volatility_zscore=c.volatility_zscore,
        liquidity_quote=c.liquidity_quote,
        instrument=INSTRUMENT,
        equity=c.equity,
        cash=c.cash,
    )


def _check(verdict: object, name: str) -> object:
    return next(c for c in verdict.checks if c.name == name)  # type: ignore[attr-defined]


async def test_r16_blocks_entry_from_db_pause_but_not_close(db: sqlite3.Connection) -> None:
    await pipeline(db, [FakeFetcher([raw(HACK)])]).run_once()
    builder = RiskContextBuilder(db, configured_mode=make_settings().trading_mode)
    gate = RiskGate(RiskLimits())
    buy = make_signal(SignalAction.BUY)
    ctx = builder.build(buy, _market(), now=NOW)
    assert ctx.news_paused_symbols == frozenset({"BTC/USDT"})
    v = await gate.evaluate(buy, ctx)
    assert not v.approved and "symbol BTC/USDT paused" in (v.risk_reason or "")
    close = make_signal(SignalAction.CLOSE)
    assert _check(
        await gate.evaluate(close, builder.build(close, _market(), now=NOW)), "news_pause"
    ).passed  # type: ignore[attr-defined]
    # пауза истекает
    later = builder.build(buy, _market(), now=NOW + timedelta(hours=2))
    assert later.news_paused_symbols == frozenset()


def test_news_flags_soft_reduce_and_blocking(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO news(id, source, content_hash, title, published_ts, fetched_ts) "
        "VALUES ('n1', 'finnhub', 'h', 't', 1, 1)"
    )
    db.execute(
        "INSERT INTO news_reactions(id, rule, action, symbol, flag_code, flag_severity, news_id, "
        "created_ts, expires_ts) VALUES ('r1', 'mixed', 'add_risk_flag', 'BTC/USDT', "
        "'news_mixed', 'medium', 'n1', ?, ?)",
        (NOW_MS - MIN, NOW_MS + MIN),
    )
    db.execute(
        "INSERT INTO news_reactions(id, rule, action, symbol, flag_code, flag_severity, news_id, "
        "created_ts, expires_ts) VALUES ('r2', 'eth', 'add_risk_flag', 'ETH/USDT', "
        "'news_halt', 'medium', 'n1', ?, ?)",
        (NOW_MS - MIN, NOW_MS + MIN),
    )
    builder = RiskContextBuilder(db, configured_mode=make_settings().trading_mode)
    sig = make_signal(SignalAction.BUY, stop_loss=Decimal("95"))
    ctx = builder.build(sig, _market(), now=NOW)
    assert ctx.news_flags == (RiskFlag(code="news_mixed", severity="medium"),)  # только свой символ
    gate = RiskGate(RiskLimits())
    base = gate.size(sig, make_ctx())
    reduced = gate.size(sig, ctx)
    assert reduced.soft_codes == ["news_mixed"]
    assert reduced.qty is not None and base.qty is not None and reduced.qty < base.qty
    # news_halt (blocking) через ctx.news_flags отклоняет вход по R15
    halted = make_ctx(news_flags=(RiskFlag(code="news_halt", severity="low"),))
    assert not _check(gate.evaluate_sync(sig, halted), "risk_flags").passed  # type: ignore[attr-defined]


def test_pause_strategy_matches_name_and_version(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO news(id, source, content_hash, title, published_ts, fetched_ts) "
        "VALUES ('n1', 'finnhub', 'h', 't', 1, 1)"
    )
    db.execute(
        "INSERT INTO news_reactions(id, rule, action, strategy, news_id, created_ts, expires_ts) "
        "VALUES ('r1', 'x', 'pause_strategy', 'sma_cross', 'n1', ?, ?)",
        (NOW_MS - MIN, NOW_MS + MIN),
    )
    builder = RiskContextBuilder(db, configured_mode=make_settings().trading_mode)
    sig = make_signal()
    builder.signal_strategies = lambda _s: frozenset({"sma_cross@1.0.0", "rsi@1.0.0"})  # type: ignore[method-assign]
    _, paused, _ = builder.news_effects(sig, NOW)
    assert paused == frozenset({"sma_cross@1.0.0"})


# --- CLI ------------------------------------------------------------------------------


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, FakeFetcher]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("SYMBOLS", '["BTC/USDT", "ETH/USDT"]')
    db = str(tmp_path / "augur.db")
    Migrator(backup_dir=tmp_path / "b", app_version="0.8.0").migrate(db)
    now = datetime.now(UTC)
    fetcher = FakeFetcher(
        [
            raw(HACK, published_at=now - timedelta(minutes=2)),
            raw("Ether gains", published_at=now - timedelta(minutes=3)),
        ]
    )
    monkeypatch.setattr("augur.interfaces.cli.main._news_fetchers", lambda _s: [fetcher])
    return db, fetcher


def test_cli_news_fetch_list_reactions_lift(cli: tuple[str, FakeFetcher]) -> None:
    db, _ = cli
    runner = CliRunner()
    res = runner.invoke(app, ["news", "fetch", "--db", db])
    assert res.exit_code == 0, res.output
    assert (
        "inserted 2" in res.output and "REACTION strong_negative_pause: pause_symbol" in res.output
    )

    res = runner.invoke(app, ["news", "list", "--db", db, "--symbol", "ETH/USDT"])
    assert res.exit_code == 0 and "Ether gains" in res.output and HACK not in res.output

    res = runner.invoke(app, ["news", "reactions", "--db", db])
    assert res.exit_code == 0 and "pause_symbol BTC/USDT" in res.output
    rid = res.output.split()[0]

    assert runner.invoke(app, ["news", "lift", rid, "--db", db]).exit_code == 2  # без --confirm
    res = runner.invoke(app, ["news", "lift", rid, "--confirm", "--db", db])
    assert res.exit_code == 0 and f"lifted {rid}" in res.output
    res = runner.invoke(app, ["news", "reactions", "--db", db])
    assert "no active news reactions" in res.output
    assert runner.invoke(app, ["news", "lift", rid, "--confirm", "--db", db]).exit_code == 1


def test_cli_news_fetch_all_sources_failed(cli: tuple[str, FakeFetcher]) -> None:
    db, fetcher = cli
    fetcher.error = NewsFetchError("finnhub HTTP 401 (crypto)", retryable=False)
    res = CliRunner().invoke(app, ["news", "fetch", "--db", db])
    assert res.exit_code == 1 and "finnhub HTTP 401" in res.output
