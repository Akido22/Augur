"""Запись в append-only `audit_log` с hash-chain (ARCHITECTURE.md §3.2, §3.5).

`row_hash = sha256(prev_hash + canonical_json(row))`. Полноценный `AuditRepository`
с `DbWriter` (single writer) — этап 0.6.0; здесь — синхронный минимум для Data Layer.
"""

import hashlib
import json
import sqlite3
import time
from typing import Any


def append_audit(
    conn: sqlite3.Connection,
    *,
    actor: str,
    event: str,
    severity: str,
    ref_type: str | None = None,
    ref_id: str | None = None,
    payload: dict[str, Any] | None = None,
    ts: int | None = None,
) -> str:
    """Добавляет строку в `audit_log` (без COMMIT — транзакцией управляет вызывающий).

    Returns:
        `row_hash` добавленной строки.
    """
    ts = ts if ts is not None else int(time.time() * 1000)
    payload_json = json.dumps(
        payload or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    prev = conn.execute("SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash: str | None = prev[0] if prev else None
    row = {
        "ts": ts,
        "actor": actor,
        "event": event,
        "severity": severity,
        "ref_type": ref_type,
        "ref_id": ref_id,
        "payload_json": payload_json,
    }
    canonical = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    row_hash = hashlib.sha256(((prev_hash or "") + canonical).encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO audit_log(ts, actor, event, severity, ref_type, ref_id, payload_json, "
        "prev_hash, row_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, actor, event, severity, ref_type, ref_id, payload_json, prev_hash, row_hash),
    )
    return row_hash
