"""LLM explain → decision_audit.explanation (FUSION SPEC §3.1, §6.2 п. 8) и переходы LLM-breaker."""

import json
import re
import sqlite3

from pydantic import SecretStr

from augur.config.settings import LLMProvider, LLMSettings
from augur.fusion.llm.breaker import breaker_state, sync_breaker_states
from augur.fusion.llm.client import LLMHTTPError
from augur.fusion.llm.explain import ExplainService
from augur.strategies.base import SignalAction
from tests._exec import Clock, classic_signal, process, stack
from tests.fusion.llm._fake import FakeLLM

KEY = SecretStr("sk-test-0123456789abcdefghij")
PROVIDERS = [
    LLMProvider(name="p1", base_url="https://p1.example/v1", model="m1", api_key="anthropic")
]


def _cfg(**over: object) -> LLMSettings:
    base: dict[str, object] = {"mode": "enabled", "providers": PROVIDERS, "daily_usd": 10.0}
    base.update(over)
    return LLMSettings(**base)


def _svc(db: sqlite3.Connection, fake: FakeLLM, clock: Clock, **over: object) -> ExplainService:
    return ExplainService(db, _cfg(**over), fake, {"anthropic": KEY}, clock=clock)  # type: ignore[arg-type]


def _reply(system: str, user: str) -> str:
    decision_id = re.search(r'decision_id="([0-9A-Z]{26})"', user)
    assert decision_id is not None
    return json.dumps(
        {
            "schema_version": "1.0",
            "decision_id": decision_id.group(1),
            "summary": "Вход по сигналу sma_cross, Risk Gate одобрил.",
            "key_factors": ["сигнал стратегии BUY", "лимиты не превышены"],
            "caveats": [],
        }
    )


async def _decision(
    db: sqlite3.Connection, action: SignalAction = SignalAction.BUY
) -> tuple[str, Clock]:
    st, clock = stack(db)
    sig = await classic_signal(db, action, now_ms=clock.now)
    res = await process(st, clock, sig)
    return res.decision.id, clock


async def test_explain_writes_once_and_never_retries(db: sqlite3.Connection) -> None:
    decision_id, clock = await _decision(db)
    fake = FakeLLM([_reply])
    svc = _svc(db, fake, clock)
    assert svc.pending(10) == [decision_id]
    out = await svc.explain_pending()
    assert [r.status for r in out] == ["ok"]
    doc = json.loads(
        db.execute(
            "SELECT explanation FROM decision_audit WHERE id = ?", (decision_id,)
        ).fetchone()[0]
    )
    assert doc["prompt_version"] == "prompt_explain_v1" and doc["key_factors"]
    assert svc.pending(10) == []  # одна попытка на решение
    prompt = fake.calls[0]["user"]
    assert "client_order_id" not in prompt and KEY.get_secret_value() not in prompt
    row = db.execute(
        "SELECT task, request_group, status FROM llm_calls WHERE task = 'explain'"
    ).fetchone()
    assert row == ("explain", decision_id, "ok")


async def test_explain_only_for_enter_exit_reduce(db: sqlite3.Connection) -> None:
    _, clock = await _decision(db, SignalAction.HOLD)
    assert _svc(db, FakeLLM([_reply]), clock).pending(10) == []


async def test_invalid_explanation_is_rejected_not_repaired(db: sqlite3.Connection) -> None:
    decision_id, clock = await _decision(db)
    bad = json.dumps(
        {
            "schema_version": "1.0",
            "decision_id": decision_id,
            "summary": "s",
            "key_factors": ["f"],
            "action": "BUY",  # поле действия запрещено схемой
        }
    )
    wrong_id = json.dumps(
        {"schema_version": "1.0", "decision_id": "X" * 26, "summary": "s", "key_factors": ["f"]}
    )
    fake = FakeLLM([bad, wrong_id])
    res = await _svc(db, fake, clock).explain(decision_id)
    assert res.status == "rejected" and len(res.llm_call_ids) == 2  # один повтор
    assert "отклонён проверкой схемы" in fake.calls[1]["user"]
    assert db.execute(
        "SELECT explanation FROM decision_audit WHERE id = ?", (decision_id,)
    ).fetchone() == (None,)
    statuses = [r[0] for r in db.execute("SELECT status FROM llm_calls ORDER BY id")]
    assert statuses == ["invalid_schema", "ungrounded"]


async def test_explain_skipped_on_budget_and_disabled(db: sqlite3.Connection) -> None:
    decision_id, clock = await _decision(db)
    db.execute(
        "INSERT INTO llm_calls(prompt_hash, provider, model, input_tokens, output_tokens, "
        "latency_ms, cost_usd, created_at, task, status) VALUES ('h','p','m',0,0,0,9.0,?, "
        "'context','ok')",
        (clock.now,),
    )
    db.commit()
    res = await _svc(db, FakeLLM([_reply]), clock).explain(decision_id)
    assert res.status == "skipped:budget_event_only"
    assert _svc(db, FakeLLM([_reply]), clock).pending(10) == [decision_id]  # вернётся в очередь
    assert await _svc(db, FakeLLM([_reply]), clock, explain_enabled=False).explain_pending() == []


async def test_provider_failure_leaves_no_explanation(db: sqlite3.Connection) -> None:
    decision_id, clock = await _decision(db)
    fake = FakeLLM([LLMHTTPError("timeout", "t", latency_ms=8000)])
    res = await _svc(db, fake, clock).explain(decision_id)
    assert res.status == "unavailable"


# --- LLM-breaker ---------------------------------------------------------------------


def _calls(db: sqlite3.Connection, n: int, status: str, ts: int) -> None:
    for _ in range(n):
        db.execute(
            "INSERT INTO llm_calls(prompt_hash, provider, model, input_tokens, output_tokens, "
            "latency_ms, cost_usd, created_at, task, status) VALUES ('h','p','m',0,0,0,0,?, "
            "'context',?)",
            (ts, status),
        )
    db.commit()


def test_breaker_transitions_are_audited(db: sqlite3.Connection) -> None:
    now = 1_700_000_000_000
    cfg = _cfg()
    _calls(db, 7, "ok", now)
    _calls(db, 3, "invalid_schema", now)  # 30 % > 20 %, 10 вызовов
    assert sync_breaker_states(db, cfg, clock=lambda: now) == [("context", True)]
    assert breaker_state(db, "context")
    assert sync_breaker_states(db, cfg, clock=lambda: now + 1) == []  # без повторов
    later = now + 2 * 3_600_000
    assert sync_breaker_states(db, cfg, clock=lambda: later) == [("context", False)]
    events = [
        r[0]
        for r in db.execute(
            "SELECT event FROM audit_log WHERE event LIKE 'LLM_BREAKER_%' ORDER BY id"
        )
    ]
    assert events == ["LLM_BREAKER_OPENED", "LLM_BREAKER_CLOSED"]
