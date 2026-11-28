"""Алерты (0.11.0, ARCH §2.11): audit_log → notifications → Telegram, ретраи, без утечки токена."""

import json
import sqlite3

import httpx
import pytest
from pydantic import SecretStr

from augur.config.settings import NotifySettings
from augur.db.audit import append_audit
from augur.notify import (
    AlertDispatcher,
    Notification,
    NotifyError,
    TelegramNotifier,
    notification_from_audit,
)
from tests.conftest import make_settings

TOKEN = "123456:SECRET-TOKEN"  # noqa: S105 — тестовое значение


class Clock:
    def __init__(self, now: int = 1_700_000_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class FakeNotifier:
    def __init__(self, fail: int = 0) -> None:
        self.sent: list[Notification] = []
        self.fail = fail

    async def send(self, msg: Notification) -> None:
        if self.fail > 0:
            self.fail -= 1
            raise NotifyError("http 502")
        self.sent.append(msg)


def _audit(db: sqlite3.Connection, event: str, severity: str = "WARN", **payload: object) -> None:
    append_audit(db, actor="test", event=event, severity=severity, payload=payload, ts=1)
    db.commit()


def _rows(db: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return db.execute("SELECT kind, level, status FROM notifications ORDER BY id").fetchall()


async def test_collect_selects_alert_events(db: sqlite3.Connection) -> None:
    _audit(db, "CB_TRIPPED", "CRITICAL", trigger="drawdown", detail="12%")
    _audit(db, "RISK_REJECTED", "WARN")  # не алерт
    _audit(db, "MODE_CHANGED", **{"from": "MICRO_LIVE", "to": "FULL_AUTO", "approved_by": "A"})
    _audit(db, "SOMETHING_BROKE", "ERROR")  # ERROR без правила → CRITICAL
    _audit(db, "LLM_BREAKER_CLOSED", "INFO")  # INFO ниже min_level=WARN
    d = AlertDispatcher(db, NotifySettings(), FakeNotifier(), clock=Clock())
    assert d.collect() == 3
    assert d.collect() == 0  # курсор сдвинут, дублей нет
    assert _rows(db) == [
        ("CB_TRIPPED", "CRITICAL", "pending"),
        ("MODE_CHANGED", "CRITICAL", "pending"),  # реальные деньги без человека
        ("SOMETHING_BROKE", "CRITICAL", "pending"),
    ]
    title, body = db.execute(
        "SELECT title, body FROM notifications WHERE kind='MODE_CHANGED'"
    ).fetchone()
    assert title == "Trading mode MICRO_LIVE -> FULL_AUTO" and "approved_by: A" in body


async def test_dispatch_sends_and_retries_then_fails(db: sqlite3.Connection) -> None:
    clock = Clock()
    cfg = NotifySettings(max_attempts=3, retry_base_s=10)
    _audit(db, "KILL_SWITCH", "CRITICAL")
    notifier = FakeNotifier(fail=1)
    d = AlertDispatcher(db, cfg, notifier, clock=clock)
    rep = await d.dispatch()
    assert rep.collected == 1 and rep.retried == 1 and rep.sent == 0
    row = db.execute("SELECT status, attempts, next_attempt_ts, last_error FROM notifications")
    assert row.fetchone() == ("pending", 1, clock.now + 10_000, "http 502")
    assert (await d.dispatch()).sent == 0  # ещё рано
    clock.now += 10_000
    assert (await d.dispatch()).sent == 1
    assert notifier.sent[0].kind == "KILL_SWITCH" and notifier.sent[0].level == "CRITICAL"

    _audit(db, "CB_TRIPPED", "CRITICAL")
    always = FakeNotifier(fail=100)
    d2 = AlertDispatcher(db, cfg, always, clock=clock)
    for _ in range(3):
        await d2.dispatch()
        clock.now += 100_000
    assert db.execute(
        "SELECT status, attempts FROM notifications WHERE kind='CB_TRIPPED'"
    ).fetchone() == ("failed", 3)


async def test_unconfigured_channel_marks_skipped(db: sqlite3.Connection) -> None:
    _audit(db, "RECONCILE_MISMATCH", "CRITICAL")
    rep = await AlertDispatcher(db, NotifySettings(), None, clock=Clock()).dispatch()
    assert rep.skipped == 1 and _rows(db)[0][2] == "skipped"


def test_history_before_migration_is_not_sent() -> None:
    from tests.conftest import MIGRATIONS_DIR

    conn = sqlite3.connect(":memory:")
    for path in sorted(MIGRATIONS_DIR.glob("000[1-7]_*.sql")):
        conn.executescript(path.read_text(encoding="utf-8"))
    _audit(conn, "CB_TRIPPED", "CRITICAL")
    conn.executescript((MIGRATIONS_DIR / "0008_notify.sql").read_text(encoding="utf-8"))
    assert AlertDispatcher(conn, NotifySettings(), None, clock=Clock()).collect() == 0
    conn.close()


def test_notification_from_audit_ignores_bad_payload() -> None:
    n = notification_from_audit(
        event="CB_TRIPPED",
        severity="CRITICAL",
        actor="cb",
        ref_type="cb",
        ref_id="drawdown",
        payload_json="{not json",
        ts_iso="t",
    )
    assert n is not None and "ref: cb:drawdown" in n.body
    long = notification_from_audit(
        event="NEWS_REACTION",
        severity="WARN",
        actor="news",
        ref_type=None,
        ref_id=None,
        payload_json=json.dumps({"title": "x" * 5000}),
        ts_iso="t",
    )
    assert long is not None and len(long.body) < 1000  # значения обрезаются


# --- Telegram ------------------------------------------------------------------------


async def test_telegram_sends_plain_text() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    tg = TelegramNotifier(SecretStr(TOKEN), "42", transport=httpx.MockTransport(handler))
    await tg.send(Notification(level="WARN", kind="X", title="T", body="B"))
    assert seen["path"] == f"/bot{TOKEN}/sendMessage"
    body = seen["body"]
    assert isinstance(body, dict) and body["chat_id"] == "42" and body["text"] == "[WARN] T\nB"
    assert "parse_mode" not in body
    assert TOKEN not in repr(tg)


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (lambda req: httpx.Response(401, json={"ok": False}), "http 401"),
        (lambda req: httpx.Response(200, json={"ok": False}), "telegram ok=false"),
    ],
)
async def test_telegram_errors_hide_token(handler: object, expected: str) -> None:
    tg = TelegramNotifier(SecretStr(TOKEN), "42", transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    with pytest.raises(NotifyError) as ei:
        await tg.send(Notification(level="INFO", kind="X", title="T", body="B"))
    assert str(ei.value) == expected and TOKEN not in str(ei.value)


async def test_telegram_network_error_hides_url() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {req.url}")

    tg = TelegramNotifier(SecretStr(TOKEN), "42", transport=httpx.MockTransport(handler))
    with pytest.raises(NotifyError) as ei:
        await tg.send(Notification(level="INFO", kind="X", title="T", body="B"))
    assert str(ei.value) == "ConnectError"
    assert ei.value.__cause__ is None and TOKEN not in repr(ei.value)


def test_build_notifier_requires_token_and_chat() -> None:
    from augur.notify import build_notifier

    assert build_notifier(make_settings()) is None
    s = make_settings(telegram_bot_token=SecretStr(TOKEN), telegram_chat_id="1")
    assert isinstance(build_notifier(s), TelegramNotifier)
    off = make_settings(
        telegram_bot_token=SecretStr(TOKEN),
        telegram_chat_id="1",
        notify=NotifySettings(enabled=False),
    )
    assert build_notifier(off) is None
