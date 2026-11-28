"""Fusion-breaker по активу (ML_LLM_FUSION_SPEC.md §5.4; 0.12.0).

Триггер в 0.12.0 — дрейф ML (PSI, nightly job): «Fusion OPEN для актива, задача на
переобучение; сброс вручную после разбора». Пока breaker актива OPEN, `LocalFusionClient`
передаёт в `fuse()` `breaker_open=True` → правило §4.3 п. 1: `HOLD` (`circuit_breaker_open`),
новых входов по активу нет; закрытие позиций — за Risk Engine (CLOSE/REDUCE не блокируются).

Состояние хранится в append-only `audit_log` (как LLM-breaker, 0.11.0): последнее событие
`FUSION_BREAKER_OPENED` / `FUSION_BREAKER_CLOSED` с `ref_type='fusion_asset'`, `ref_id=<asset>`.
Миграция не нужна, каждый переход — запись в аудите и алерт. Автоматического сброса нет:
только `augur fusion breaker reset --symbol S --approved-by <имя> --confirm` с кодом.
"""

import json
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from augur.db.audit import append_audit

__all__ = [
    "CLOSED",
    "OPENED",
    "REF_TYPE",
    "AssetBreakerStatus",
    "asset_breaker_open",
    "asset_breaker_status",
    "close_asset_breaker",
    "list_open_breakers",
    "open_asset_breaker",
]

OPENED, CLOSED = "FUSION_BREAKER_OPENED", "FUSION_BREAKER_CLOSED"
REF_TYPE = "fusion_asset"


class AssetBreakerStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    asset: str
    state: Literal["OPEN", "CLOSED"]
    trigger: str | None = None
    since_ts: int | None = None  # время последнего перехода
    actor: str | None = None
    detail: dict[str, Any] = {}


def _status(asset: str, row: tuple[Any, ...] | None) -> AssetBreakerStatus:
    if row is None:
        return AssetBreakerStatus(asset=asset, state="CLOSED")
    event, ts, actor, payload = row
    data = json.loads(str(payload or "{}"))
    return AssetBreakerStatus(
        asset=asset,
        state="OPEN" if event == OPENED else "CLOSED",
        trigger=data.get("trigger"),
        since_ts=int(ts),
        actor=str(actor),
        detail=data,
    )


def asset_breaker_status(conn: sqlite3.Connection, asset: str) -> AssetBreakerStatus:
    row = conn.execute(
        "SELECT event, ts, actor, payload_json FROM audit_log WHERE ref_type=? AND ref_id=? "
        "AND event IN (?, ?) ORDER BY id DESC LIMIT 1",
        (REF_TYPE, asset, OPENED, CLOSED),
    ).fetchone()
    return _status(asset, row)


def asset_breaker_open(conn: sqlite3.Connection, asset: str) -> bool:
    return asset_breaker_status(conn, asset).state == "OPEN"


def list_open_breakers(conn: sqlite3.Connection) -> list[AssetBreakerStatus]:
    rows = conn.execute(
        "SELECT a.ref_id, a.event, a.ts, a.actor, a.payload_json FROM audit_log a "
        "WHERE a.ref_type=? AND a.event IN (?, ?) AND a.id = (SELECT MAX(b.id) FROM audit_log b "
        "WHERE b.ref_type=a.ref_type AND b.ref_id=a.ref_id AND b.event IN (?, ?)) "
        "ORDER BY a.ref_id",
        (REF_TYPE, OPENED, CLOSED, OPENED, CLOSED),
    ).fetchall()
    out = [_status(str(r[0]), tuple(r[1:])) for r in rows]
    return [s for s in out if s.state == "OPEN"]


def open_asset_breaker(
    conn: sqlite3.Connection,
    asset: str,
    *,
    trigger: str,
    detail: dict[str, Any],
    actor: str,
    ts: int,
) -> bool:
    """OPEN по активу (без COMMIT). Уже OPEN → False, повторной записи нет."""
    if asset_breaker_open(conn, asset):
        return False
    append_audit(
        conn,
        actor=actor,
        event=OPENED,
        severity="WARN",
        ref_type=REF_TYPE,
        ref_id=asset,
        payload={"asset": asset, "trigger": trigger, **detail},
        ts=ts,
    )
    return True


def close_asset_breaker(
    conn: sqlite3.Connection,
    asset: str,
    *,
    actor: str,
    approved_by: str,
    reason: str,
    ts: int,
) -> bool:
    """CLOSED по активу — только человеком (без COMMIT). Не OPEN → False."""
    st = asset_breaker_status(conn, asset)
    if st.state != "OPEN":
        return False
    append_audit(
        conn,
        actor=actor,
        event=CLOSED,
        severity="INFO",
        ref_type=REF_TYPE,
        ref_id=asset,
        payload={
            "asset": asset,
            "trigger": st.trigger,
            "approved_by": approved_by,
            "reason": reason,
            "opened_ts": st.since_ts,
        },
        ts=ts,
    )
    return True
