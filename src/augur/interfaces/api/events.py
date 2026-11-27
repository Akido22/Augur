"""События для `/ws/events` без оркестратора (ARCHITECTURE.md §9.1; решение 0.9.0).

Оркестратора и шины событий пока нет (ARCH §2.12), и CLI-команды пишут в БД из других
процессов. Поэтому источник событий — БД: `EventTail` раз в `poll_s` читает новые строки
`audit_log`, итоговые сигналы, новости и изменённые позиции и превращает их в `EventDTO`.
Только чтение.
"""

import json
import sqlite3
from typing import Any

from augur.interfaces.dto import EventDTO
from augur.interfaces.queries import ReadService

__all__ = ["EventTail", "event_type_for"]

_PREFIX_TYPES: tuple[tuple[str, str], ...] = (
    ("ORDER_", "order_update"),
    ("EXECUTION_", "order_update"),
    ("RECONCILE_", "order_update"),
    ("RISK_", "risk_verdict"),
    ("CB_", "cb_state"),
    ("KILL_SWITCH", "cb_state"),
    ("MODE_", "mode_changed"),
    ("NEWS_", "news_event"),
)


def event_type_for(audit_event: str) -> str:
    """Тип WS-сообщения по имени события `audit_log`; неизвестное — `audit`."""
    for prefix, kind in _PREFIX_TYPES:
        if audit_event.startswith(prefix):
            return kind
    return "audit"


class EventTail:
    """Курсоры по таблицам; `poll()` возвращает события, появившиеся после прошлого вызова.

    Первый `prime()` ставит курсоры на текущий конец: история не пересылается, клиент берёт
    снимок через REST.
    """

    def __init__(self) -> None:
        self.audit_id = 0
        self.signal_id = ""
        self.news_id = ""
        self.position_ts = 0

    def prime(self, conn: sqlite3.Connection) -> None:
        self.audit_id = int(
            conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0]
        )
        self.signal_id = str(
            conn.execute("SELECT COALESCE(MAX(id), '') FROM signals WHERE kind='final'").fetchone()[
                0
            ]
        )
        self.news_id = str(conn.execute("SELECT COALESCE(MAX(id), '') FROM news").fetchone()[0])
        self.position_ts = int(
            conn.execute("SELECT COALESCE(MAX(updated_ts), 0) FROM positions").fetchone()[0]
        )

    def poll(self, conn: sqlite3.Connection, *, limit: int = 200) -> list[EventDTO]:
        out: list[EventDTO] = []
        svc = ReadService(conn)
        for ev in svc.audit_events(after_id=self.audit_id, limit=limit):
            self.audit_id = ev.id
            out.append(EventDTO(type=event_type_for(ev.event), data=ev.model_dump()))  # type: ignore[arg-type]
        # итоговые сигналы: ULID монотонен по времени создания
        for sid, symbol, action, source, strength in conn.execute(
            "SELECT id, symbol, action, source, strength FROM signals "
            "WHERE kind='final' AND id > ? ORDER BY id LIMIT ?",
            (self.signal_id, limit),
        ):
            self.signal_id = sid
            out.append(
                EventDTO(
                    type="signal_ready",
                    data={
                        "id": sid,
                        "symbol": symbol,
                        "action": action,
                        "source": source,
                        "strength": strength,
                    },
                )
            )
        for nid, title, symbols_json, sentiment, importance, published_ts in conn.execute(
            "SELECT id, title, symbols_json, sentiment, importance, published_ts FROM news "
            "WHERE id > ? ORDER BY id LIMIT ?",
            (self.news_id, limit),
        ):
            self.news_id = nid
            out.append(
                EventDTO(
                    type="news_event",
                    data={
                        "id": nid,
                        "title": title,
                        "symbols": _loads(symbols_json, []),
                        "sentiment": sentiment,
                        "importance": importance,
                        "published_ts": published_ts,
                    },
                )
            )
        for venue, symbol, mode, qty, avg_price, updated_ts in conn.execute(
            "SELECT venue, symbol, mode, qty, avg_price, updated_ts FROM positions "
            "WHERE updated_ts > ? ORDER BY updated_ts",
            (self.position_ts,),
        ):
            self.position_ts = max(self.position_ts, int(updated_ts))
            out.append(
                EventDTO(
                    type="position_update",
                    data={
                        "venue": venue,
                        "symbol": symbol,
                        "mode": mode,
                        "qty": qty,
                        "avg_price": avg_price,
                        "updated_ts": updated_ts,
                    },
                )
            )
        return out


def _loads(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw) if raw is not None else default
    except ValueError:
        return default
