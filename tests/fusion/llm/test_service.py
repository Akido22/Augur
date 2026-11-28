"""LLMContextService: сэмплы, повтор, fallback провайдеров, бюджет, breaker, llm_calls (§3–§5)."""

import json
import sqlite3
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from augur.config.settings import LLMProvider, LLMSettings, LLMVarianceSettings
from augur.fusion.llm.client import LLMHTTPError, OpenAICompatClient
from augur.fusion.llm.inputs import NewsInput, PriceSummary, load_news_inputs
from augur.fusion.llm.service import LLMContextService
from tests.fusion.llm._fake import FakeLLM, ctx_json

AS_OF = int(datetime(2026, 11, 20, 8, tzinfo=UTC).timestamp() * 1000)
KEY = SecretStr("sk-test-0123456789abcdefghij")
PROVIDERS = [
    LLMProvider(name="p1", base_url="https://p1.example/v1", model="m1", api_key="anthropic"),
    LLMProvider(
        name="p2",
        base_url="https://p2.example/v1",
        model="m2",
        api_key="deepseek",
        price_per_mtok={"input": 1.0, "output": 2.0},
    ),  # type: ignore[arg-type]
]
NEWS = [
    NewsInput(
        news_id="news_1",
        published_ts=AS_OF - 3_600_000,
        source="finnhub",
        title="Exchange delays withdrawals",
        body="body",
    ),
    NewsInput(
        news_id="news_2",
        published_ts=AS_OF - 1_800_000,
        source="finnhub",
        title="ETF inflows",
        body="body",
    ),
]
PRICE = PriceSummary(window_h=6, ret=0.01, vol_z=0.5)


def _svc(db: sqlite3.Connection, fake: FakeLLM, **cfg: object) -> LLMContextService:
    base: dict[str, object] = {"mode": "enabled", "providers": PROVIDERS, "daily_usd": 10.0}
    base.update(cfg)
    return LLMContextService(
        db,
        LLMSettings(**base),
        fake,  # type: ignore[arg-type]
        {"anthropic": KEY, "deepseek": KEY},
        clock=lambda: AS_OF + 5,
    )


def _calls(db: sqlite3.Connection) -> list[tuple[str, str, int | None]]:
    return db.execute("SELECT provider, status, sample_index FROM llm_calls ORDER BY id").fetchall()


async def test_ok_three_samples_aggregated_and_logged(db: sqlite3.Connection) -> None:
    flag = [{"code": "exchange_incident", "severity": "medium", "evidence_ids": ["news_1"]}]
    fake = FakeLLM([ctx_json(0.1, flags=flag), ctx_json(0.2, flags=flag), ctx_json(0.3)])
    res = await _svc(db, fake).refresh("BTC/USDT", as_of=AS_OF, news=NEWS, price=PRICE)
    assert res.status == "ok" and res.context is not None
    assert res.context.context_score == 0.2 and len(res.llm_call_ids) == 3
    assert [f.code.value for f in res.context.risk_flags] == ["exchange_incident"]
    assert _calls(db) == [("p1", "ok", 0), ("p1", "ok", 1), ("p1", "ok", 2)]
    # разный порядок новостей в сэмплах; ни ML, ни позиции в промпте (§4.4)
    users = [c["user"] for c in fake.calls]
    assert users[0].index("news_1") < users[0].index("news_2")
    assert users[1].index("news_2") < users[1].index("news_1")
    assert all("lightgbm" not in u and "position" not in u for u in users)
    latest = _svc(db, fake).latest("BTC/USDT", now_ms=AS_OF + 10, max_age_s=1800)
    assert latest is not None and latest.context == res.context
    assert latest.context_id == res.context_id
    row = db.execute("SELECT kind, version FROM artifacts WHERE kind='llm_prompt'").fetchone()
    assert row == ("llm_prompt", "prompt_context_v1")


async def test_repair_once_with_error_text_then_accept(db: sqlite3.Connection) -> None:
    fake = FakeLLM(["not json", ctx_json(0.0, 0.2)])
    res = await _svc(db, fake).refresh("BTC/USDT", as_of=AS_OF, news=[], price=PRICE)
    assert res.status == "ok"
    assert [s for _, s, _ in _calls(db)] == ["invalid_schema", "ok"]
    assert "not json" not in fake.calls[1]["user"]  # без исходного ответа
    assert "отклонён" in fake.calls[1]["user"]


async def test_ungrounded_everywhere_is_rejected(db: sqlite3.Connection) -> None:
    bad = ctx_json(
        flags=[{"code": "exchange_incident", "severity": "high", "evidence_ids": ["news_404"]}]
    )
    res = await _svc(db, FakeLLM([bad])).refresh("BTC/USDT", as_of=AS_OF, news=NEWS, price=PRICE)
    assert res.status == "rejected" and res.context is None
    assert res.reject_reason == "too_few_valid_samples"
    assert {s for _, s, _ in _calls(db)} == {"ungrounded"}
    assert db.execute("SELECT event FROM audit_log").fetchall() == [("LLM_FALLBACK",)]


async def test_variance_reject(db: sqlite3.Connection) -> None:
    fake = FakeLLM([ctx_json(-0.8), ctx_json(0.0), ctx_json(0.8)])
    res = await _svc(db, fake).refresh("BTC/USDT", as_of=AS_OF, news=NEWS, price=PRICE)
    assert (res.status, res.reject_reason) == ("rejected", "score_variance")


async def test_provider_fallback_then_ml_only(db: sqlite3.Connection) -> None:
    fake = FakeLLM({"p1": [LLMHTTPError("timeout", "t", 8000)], "p2": [ctx_json(0.0, 0.2)]})
    res = await _svc(db, fake).refresh("BTC/USDT", as_of=AS_OF, news=[], price=PRICE)
    assert res.status == "ok"
    assert _calls(db) == [("p1", "timeout", 0), ("p2", "ok", 0)]
    cost = db.execute("SELECT cost_usd FROM llm_calls WHERE provider='p2'").fetchone()[0]
    assert cost == pytest.approx((1000 * 1.0 + 200 * 2.0) / 1e6)

    down = FakeLLM([LLMHTTPError("http_5xx", "HTTP 503", 10)])
    res2 = await _svc(db, down).refresh("ETH/USDT", as_of=AS_OF, news=[], price=PRICE)
    assert (res2.status, res2.reject_reason) == ("fallback_ml_only", "providers_unavailable")


async def test_budget_exhausted_no_network_call(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO llm_calls(prompt_hash, provider, model, latency_ms, cost_usd, created_at) "
        "VALUES ('h', 'p1', 'm1', 1, 10.5, ?)",
        (AS_OF,),
    )
    fake = FakeLLM([ctx_json()])
    res = await _svc(db, fake).refresh("BTC/USDT", as_of=AS_OF, news=NEWS, price=PRICE)
    assert (res.status, res.reject_reason) == ("fallback_ml_only", "budget_exceeded")
    assert fake.calls == []
    assert _calls(db)[-1][1] == "budget_block"


async def test_event_only_budget_and_hourly_limit(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO llm_calls(prompt_hash, provider, model, latency_ms, cost_usd, created_at) "
        "VALUES ('h', 'p1', 'm1', 1, 8.5, ?)",
        (AS_OF,),
    )
    fake = FakeLLM([ctx_json(0.0, 0.2)])
    sched = await _svc(db, fake).refresh("BTC/USDT", as_of=AS_OF, news=[], price=PRICE)
    assert sched.reject_reason == "budget_event_only" and fake.calls == []
    event = await _svc(db, fake).refresh(
        "BTC/USDT", as_of=AS_OF, news=[], price=PRICE, event_driven=True
    )
    assert event.status == "ok"
    limited = await _svc(db, fake, max_calls_per_asset_hour=1).refresh(
        "BTC/USDT", as_of=AS_OF, news=[], price=PRICE, event_driven=True
    )
    assert limited.reject_reason == "asset_hourly_limit"


async def test_secret_in_prompt_blocks_call(db: sqlite3.Connection) -> None:
    leak = [
        NewsInput(
            news_id="n",
            published_ts=AS_OF - 1,
            source="x",
            title=f"leak {KEY.get_secret_value()}",
            body="",
        )
    ]
    fake = FakeLLM([ctx_json()])
    res = await _svc(db, fake).refresh("BTC/USDT", as_of=AS_OF, news=leak, price=PRICE)
    assert res.status == "fallback_ml_only" and fake.calls == []
    rows = db.execute("SELECT status, error FROM llm_calls").fetchall()
    assert {r[0] for r in rows} == {"secret_block"}
    assert all(KEY.get_secret_value() not in (r[1] or "") for r in rows)


async def test_disabled_mode_and_breaker(db: sqlite3.Connection) -> None:
    res = await _svc(db, FakeLLM([ctx_json()]), mode="ml_only").refresh(
        "BTC/USDT", as_of=AS_OF, news=[], price=PRICE
    )
    assert (res.status, res.reject_reason) == ("fallback_ml_only", "llm_disabled")
    for _ in range(10):
        db.execute(
            "INSERT INTO llm_calls(prompt_hash, provider, model, latency_ms, created_at, task, "
            "status) VALUES ('h', 'p1', 'm1', 1, ?, 'context', 'invalid_schema')",
            (AS_OF,),
        )
    fake = FakeLLM([ctx_json()])
    res2 = await _svc(db, fake).refresh("BTC/USDT", as_of=AS_OF, news=[], price=PRICE)
    assert res2.reject_reason == "llm_breaker_open" and fake.calls == []


async def test_prompt_version_conflict_disables_llm(db: sqlite3.Connection) -> None:
    await _svc(db, FakeLLM([ctx_json(0.0, 0.2)])).refresh(
        "BTC/USDT", as_of=AS_OF, news=[], price=PRICE
    )
    other = [PROVIDERS[0].model_copy(update={"model": "other-model"})]
    fake = FakeLLM([ctx_json()])
    res = await _svc(db, fake, providers=other).refresh(
        "BTC/USDT", as_of=AS_OF, news=[], price=PRICE
    )
    assert res.reject_reason == "prompt_conflict" and fake.calls == []


def test_load_news_inputs_filters_and_orders(db: sqlite3.Connection) -> None:
    for i, (sym, ts) in enumerate(
        [
            ("BTC/USDT", AS_OF - 100),
            ("ETH/USDT", AS_OF - 50),
            ("BTC/USDT", AS_OF - 7 * 3_600_000),
            ("BTC/USDT", AS_OF - 200),
        ]
    ):
        db.execute(
            "INSERT INTO news(id, source, content_hash, title, summary, published_ts, fetched_ts, "
            "symbols_json) VALUES (?, 'finnhub', ?, ?, ?, ?, ?, ?)",
            (f"n{i}", f"{i:064d}", f"<b>t{i}</b>", "s", ts, ts, json.dumps([sym])),
        )
    items = load_news_inputs(
        db, asset="BTC/USDT", as_of=AS_OF, window_h=6, limit=20, body_chars=100
    )
    assert [n.news_id for n in items] == ["n3", "n0"]
    assert "<" not in items[0].title


async def test_openai_compat_client_request_and_errors() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        body = json.loads(req.content)
        if body["model"] == "rl":
            return httpx.Response(429)
        if body["model"] == "boom":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    client = OpenAICompatClient(transport=httpx.MockTransport(handler))
    p = LLMProvider(name="a", base_url="https://api.example/v1/", model="m")
    raw = await client.complete_json(
        p, api_key=KEY, system="s", user="u", temperature=0.5, timeout_s=1, max_tokens=10
    )
    assert (raw.text, raw.input_tokens, raw.output_tokens) == ("{}", 7, 3)
    req = seen[0]
    assert str(req.url) == "https://api.example/v1/chat/completions"
    assert req.headers["Authorization"] == f"Bearer {KEY.get_secret_value()}"
    body = json.loads(req.content)
    assert "temperature" not in body and "tools" not in body  # send_temperature=False
    assert body["response_format"] == {"type": "json_object"}
    with_t = p.model_copy(update={"send_temperature": True})
    await client.complete_json(
        with_t, api_key=None, system="s", user="u", temperature=0.5, timeout_s=1, max_tokens=10
    )
    assert json.loads(seen[1].content)["temperature"] == 0.5
    for model, kind in (("rl", "rate_limit"), ("boom", "http_5xx")):
        with pytest.raises(LLMHTTPError) as ei:
            await client.complete_json(
                p.model_copy(update={"model": model}),
                api_key=KEY,
                system="s",
                user="u",
                temperature=None,
                timeout_s=1,
                max_tokens=10,
            )
        assert ei.value.kind == kind and KEY.get_secret_value() not in str(ei.value)


def test_variance_settings_defaults_match_spec() -> None:
    v = LLMVarianceSettings()
    assert (v.samples, v.min_valid, v.max_score_std, v.max_score_range) == (3, 2, 0.25, 0.6)
