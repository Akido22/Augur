"""`NewsReactionRules` (ARCHITECTURE.md §8.4): реакции на `news_event`.

Действия только ужесточают риск:
- `pause_symbol` / `pause_strategy` → R16 блокирует открытие новых позиций (CLOSE/REDUCE — нет);
- `add_risk_flag` → флаг `{code, severity}` попадает в R15 (blocking / high) и в sizing
  (soft_flags → qty × risk_flag_multiplier) для сигналов по символу;
- `notify` → только запись в аудит и лог (модуля уведомлений ещё нет).

Эффекты живут в `news_reactions` со сроком `published_ts + duration_min`: старая новость,
пришедшая с опозданием, не ставит паузу задним числом дольше, чем на остаток срока. Каждое
применение → `audit_log: NEWS_REACTION`. Снятие человеком → `NEWS_REACTION_LIFTED`.
Без COMMIT: транзакцией управляет вызывающий (кроме `lift`, который коммитит сам).
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from augur.config.settings import NewsRule
from augur.db.audit import append_audit
from augur.ids import new_ulid
from augur.news.models import NewsEvent
from augur.news.repository import NewsRepository

__all__ = ["AppliedReaction", "NewsReactionRules", "ReactionNotFoundError", "ReactionRecord"]

log = logging.getLogger(__name__)
_MIN_MS = 60_000


class ReactionNotFoundError(LookupError):
    """Нет активной реакции с таким id (не существует, истекла или уже снята)."""


@dataclass(frozen=True)
class AppliedReaction:
    rule: str
    action: str
    symbol: str | None
    strategy: str | None
    reaction_id: str | None  # None для notify (в news_reactions не пишется)
    expires_ts: int | None


@dataclass(frozen=True)
class ReactionRecord:
    id: str
    rule: str
    action: str
    symbol: str | None
    strategy: str | None
    flag_code: str | None
    flag_severity: str | None
    news_id: str
    created_ts: int
    expires_ts: int


class NewsReactionRules:
    def __init__(self, conn: sqlite3.Connection, rules: Sequence[NewsRule]) -> None:
        self._conn = conn
        self._rules = list(rules)
        self._news = NewsRepository(conn)

    # --- применение ------------------------------------------------------------------

    def apply(self, event: NewsEvent, *, now_ms: int) -> list[AppliedReaction]:
        """Проверяет все правила для каждого символа события. Новость без символов — только
        `notify` (паузы и флаги без привязки к символу не ставятся)."""
        applied: list[AppliedReaction] = []
        pub_ms = int(event.published_at.timestamp() * 1000)
        targets: list[str | None] = list(event.symbols) or [None]
        for rule in self._rules:
            for symbol in targets:
                if symbol is None and rule.action != "notify":
                    continue
                if not self._matches(rule, event, symbol, pub_ms):
                    continue
                applied.extend(self._act(rule, event, symbol, pub_ms=pub_ms, now_ms=now_ms))
        return applied

    def _matches(self, rule: NewsRule, ev: NewsEvent, symbol: str | None, pub_ms: int) -> bool:
        w = rule.when
        conf = ev.confidence if ev.confidence is not None else 0.0
        checks = [
            w.sentiment_lt is None or ev.sentiment < w.sentiment_lt,
            w.sentiment_gt is None or ev.sentiment > w.sentiment_gt,
            w.abs_sentiment_lt is None or abs(ev.sentiment) < w.abs_sentiment_lt,
            w.confidence_gte is None or conf >= w.confidence_gte,
            w.importance_in is None or ev.importance in w.importance_in,
        ]
        if not all(checks):
            return False
        if w.window_count_gte is None:
            return True
        if symbol is None:
            return False
        count = self._news.count_window(
            symbol,
            start_ts=pub_ms - w.window_min * _MIN_MS,
            end_ts=pub_ms,
            importance_in=w.importance_in,
        )
        return count >= w.window_count_gte

    def _act(
        self, rule: NewsRule, ev: NewsEvent, symbol: str | None, *, pub_ms: int, now_ms: int
    ) -> list[AppliedReaction]:
        if rule.action == "notify":
            log.warning("news alert [%s] %s: %s", rule.name, symbol or "-", ev.title or ev.news_id)
            self._audit(
                rule, ev, symbol=symbol, strategy=None, reaction_id=None, expires=None, ts=now_ms
            )
            return [AppliedReaction(rule.name, "notify", symbol, None, None, None)]
        expires = pub_ms + rule.duration_min * _MIN_MS
        if expires <= now_ms:
            return []  # срок реакции истёк ещё до обработки новости
        out: list[AppliedReaction] = []
        strategies: list[str | None] = (
            list(rule.strategies) if rule.action == "pause_strategy" else [None]
        )
        for strategy in strategies:
            sym = None if strategy is not None else symbol
            if self._covered(rule, sym, strategy, expires, now_ms):
                continue
            rid = new_ulid(now_ms)
            flag = rule.flag
            self._conn.execute(
                "INSERT INTO news_reactions(id, rule, action, symbol, strategy, flag_code, "
                "flag_severity, news_id, created_ts, expires_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rid,
                    rule.name,
                    rule.action,
                    sym,
                    strategy,
                    flag.code if flag else None,
                    flag.severity if flag else None,
                    ev.news_id,
                    now_ms,
                    expires,
                ),
            )
            self._audit(
                rule, ev, symbol=sym, strategy=strategy, reaction_id=rid, expires=expires, ts=now_ms
            )
            out.append(AppliedReaction(rule.name, rule.action, sym, strategy, rid, expires))
        return out

    def _covered(
        self, rule: NewsRule, symbol: str | None, strategy: str | None, expires: int, now: int
    ) -> bool:
        """Уже есть активная реакция того же правила на ту же цель, действующая не меньше."""
        row = self._conn.execute(
            "SELECT 1 FROM news_reactions WHERE rule = ? AND action = ? "
            "AND symbol IS ? AND strategy IS ? AND lifted_ts IS NULL "
            "AND expires_ts > ? AND expires_ts >= ? LIMIT 1",
            (rule.name, rule.action, symbol, strategy, now, expires),
        ).fetchone()
        return row is not None

    def _audit(
        self,
        rule: NewsRule,
        ev: NewsEvent,
        *,
        symbol: str | None,
        strategy: str | None,
        reaction_id: str | None,
        expires: int | None,
        ts: int,
    ) -> None:
        payload: dict[str, object] = {
            "rule": rule.name,
            "action": rule.action,
            "event_id": ev.event_id,
            "symbol": symbol,
            "strategy": strategy,
            "sentiment": ev.sentiment,
            "confidence": ev.confidence,
            "importance": ev.importance,
            "sentiment_method": ev.sentiment_method,
            "reaction_id": reaction_id,
            "expires_ts": expires,
        }
        if rule.flag is not None:
            payload["flag"] = {"code": rule.flag.code, "severity": rule.flag.severity}
        append_audit(
            self._conn,
            actor="news",
            event="NEWS_REACTION",
            severity="WARN" if rule.action != "notify" or ev.importance == "high" else "INFO",
            ref_type="news",
            ref_id=ev.news_id,
            payload=payload,
            ts=ts,
        )

    # --- чтение и снятие --------------------------------------------------------------

    def active(self, *, now_ms: int) -> list[ReactionRecord]:
        rows = self._conn.execute(
            "SELECT id, rule, action, symbol, strategy, flag_code, flag_severity, news_id, "
            "created_ts, expires_ts FROM news_reactions "
            "WHERE lifted_ts IS NULL AND expires_ts > ? ORDER BY created_ts, id",
            (now_ms,),
        ).fetchall()
        return [
            ReactionRecord(
                id=str(r[0]),
                rule=str(r[1]),
                action=str(r[2]),
                symbol=None if r[3] is None else str(r[3]),
                strategy=None if r[4] is None else str(r[4]),
                flag_code=None if r[5] is None else str(r[5]),
                flag_severity=None if r[6] is None else str(r[6]),
                news_id=str(r[7]),
                created_ts=int(r[8]),
                expires_ts=int(r[9]),
            )
            for r in rows
        ]

    def lift(self, reaction_id: str, *, actor: str, now_ms: int) -> ReactionRecord:
        """Снять активную реакцию (человек, с аудитом). COMMIT внутри.

        Raises:
            ReactionNotFoundError: реакции нет, она истекла или уже снята.
        """
        match = [r for r in self.active(now_ms=now_ms) if r.id == reaction_id]
        if not match:
            raise ReactionNotFoundError(reaction_id)
        rec = match[0]
        try:
            self._conn.execute(
                "UPDATE news_reactions SET lifted_ts = ?, lifted_by = ? "
                "WHERE id = ? AND lifted_ts IS NULL",
                (now_ms, actor, reaction_id),
            )
            append_audit(
                self._conn,
                actor=actor,
                event="NEWS_REACTION_LIFTED",
                severity="WARN",
                ref_type="news",
                ref_id=rec.news_id,
                payload={
                    "reaction_id": rec.id,
                    "rule": rec.rule,
                    "action": rec.action,
                    "symbol": rec.symbol,
                    "strategy": rec.strategy,
                    "flag_code": rec.flag_code,
                    "expires_ts": rec.expires_ts,
                },
                ts=now_ms,
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return rec
