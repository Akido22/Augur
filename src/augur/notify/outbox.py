"""Outbox алертов: `audit_log` → `notifications` → канал (ARCHITECTURE.md §2.11).

`collect()` читает события после `notify_cursor`, отбирает алерты (`notification_from_audit`,
не ниже `notify.min_level`) и пишет их в `notifications` (UNIQUE `audit_id` — без дублей);
курсор двигается в той же транзакции. `dispatch()` отправляет созревшие `pending`:
успех → `sent`; сбой → повтор через `retry_base_s × 2^(attempt−1)`, после `max_attempts` —
`failed`; канал не настроен → `skipped`. Ошибки канала не выходят наружу: торговый контур
от алертов не зависит. Диспетчер вызывает `augur run` и `augur alerts dispatch`.
"""

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from augur.config.settings import NotifySettings, Settings
from augur.notify.models import LEVEL_RANK, Notification, notification_from_audit
from augur.notify.telegram import Notifier, NotifyError, TelegramNotifier

__all__ = ["AlertDispatcher", "DispatchReport", "build_notifier"]

log = logging.getLogger(__name__)
_SCAN = 500


class DispatchReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    collected: int = 0
    sent: int = 0
    retried: int = 0
    failed: int = 0
    skipped: int = 0


def build_notifier(settings: Settings) -> Notifier | None:
    """Telegram из `.env`; не настроен или выключен — None (алерты станут `skipped`)."""
    cfg = settings.notify
    token, chat = settings.telegram_bot_token, settings.telegram_chat_id
    if not cfg.enabled or token is None or not token.get_secret_value() or not chat:
        return None
    return TelegramNotifier(token, chat, api_base=cfg.telegram_api_base, timeout_s=cfg.timeout_s)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


class AlertDispatcher:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: NotifySettings,
        notifier: Notifier | None,
        *,
        clock: Callable[[], int],
    ) -> None:
        self._conn = conn
        self._cfg = cfg
        self._notifier = notifier
        self._clock = clock

    # --- сбор -----------------------------------------------------------------------

    def _cursor(self) -> int:
        row = self._conn.execute("SELECT last_audit_id FROM notify_cursor WHERE id = 1").fetchone()
        return 0 if row is None else int(row[0])

    def collect(self) -> int:
        """События аудита после курсора → `notifications`. Возвращает число новых алертов."""
        now = self._clock()
        cursor = self._cursor()
        rows = self._conn.execute(
            "SELECT id, ts, actor, event, severity, ref_type, ref_id, payload_json FROM audit_log "
            "WHERE id > ? ORDER BY id LIMIT ?",
            (cursor, _SCAN),
        ).fetchall()
        if not rows:
            return 0
        min_rank = LEVEL_RANK[self._cfg.min_level]
        added = 0
        try:
            for aid, ts, actor, event, severity, ref_type, ref_id, payload in rows:
                n = notification_from_audit(
                    event=str(event),
                    severity=str(severity),
                    actor=str(actor),
                    ref_type=ref_type,
                    ref_id=ref_id,
                    payload_json=str(payload or "{}"),
                    ts_iso=_iso(int(ts)),
                )
                if n is None or LEVEL_RANK[n.level] < min_rank:
                    continue
                cur = self._conn.execute(
                    "INSERT INTO notifications(audit_id, created_ts, level, kind, title, body, "
                    "next_attempt_ts) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(audit_id) DO NOTHING",
                    (int(aid), now, n.level, n.kind, n.title, n.body, now),
                )
                added += cur.rowcount
            self._conn.execute(
                "INSERT INTO notify_cursor(id, last_audit_id, updated_ts) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET last_audit_id = excluded.last_audit_id, "
                "updated_ts = excluded.updated_ts",
                (int(rows[-1][0]), now),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return added

    # --- отправка -------------------------------------------------------------------

    async def dispatch(self) -> DispatchReport:
        """Собрать новые алерты и отправить созревшие. Никогда не бросает из-за канала."""
        collected = 0
        while True:  # догнать хвост аудита порциями
            n = self.collect()
            collected += n
            if self._cursor() >= self._max_audit_id():
                break
        now = self._clock()
        due = self._conn.execute(
            "SELECT id, level, kind, title, body, attempts FROM notifications "
            "WHERE status = 'pending' AND next_attempt_ts <= ? ORDER BY id LIMIT ?",
            (now, self._cfg.batch),
        ).fetchall()
        sent = retried = failed = skipped = 0
        for nid, level, kind, title, body, attempts in due:
            if self._notifier is None:
                self._update(nid, "skipped", attempts, now, "channel not configured")
                skipped += 1
                continue
            msg = Notification(level=level, kind=kind, title=title, body=body)
            try:
                await self._notifier.send(msg)
            except NotifyError as e:
                n_att = int(attempts) + 1
                if n_att >= self._cfg.max_attempts:
                    self._update(nid, "failed", n_att, now, str(e))
                    failed += 1
                    log.error("alert %s failed after %s attempts: %s", nid, n_att, e)
                else:
                    delay = self._cfg.retry_base_s * 1000 * 2 ** (n_att - 1)
                    self._update(nid, "pending", n_att, now + delay, str(e))
                    retried += 1
                continue
            except Exception as e:  # noqa: BLE001 — канал не должен ронять торговый цикл
                n_att = int(attempts) + 1
                status = "failed" if n_att >= self._cfg.max_attempts else "pending"
                delay = self._cfg.retry_base_s * 1000 * 2 ** (n_att - 1)
                self._update(nid, status, n_att, now + delay, type(e).__name__)
                failed += status == "failed"
                retried += status == "pending"
                continue
            self._update(nid, "sent", int(attempts) + 1, now, None, sent_ts=now)
            sent += 1
        return DispatchReport(
            collected=collected, sent=sent, retried=retried, failed=failed, skipped=skipped
        )

    def _max_audit_id(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()
        return int(row[0])

    def _update(
        self,
        nid: int,
        status: str,
        attempts: int,
        next_ts: int,
        error: str | None,
        *,
        sent_ts: int | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE notifications SET status = ?, attempts = ?, next_attempt_ts = ?, "
            "last_error = ?, sent_ts = ? WHERE id = ?",
            (status, attempts, next_ts, error, sent_ts, nid),
        )
        self._conn.commit()
