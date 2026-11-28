"""Модель уведомления и правила «событие аудита → алерт» (ARCHITECTURE.md §2.11).

Источник алертов — append-only `audit_log`: всё, о чём надо знать человеку (CB, kill switch,
смена режима, расхождение реконсиляции, LLM-breaker), там уже есть. Правила ниже выбирают
события и уровень; прочие события с severity ERROR/CRITICAL тоже становятся алертами.
Тексты собираются из payload аудита (секреты туда не пишутся) и отправляются простым
текстом — без разметки, поэтому недоверенный текст новостей ничего не «исполнит».
"""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ALERT_RULES", "LEVEL_RANK", "Level", "Notification", "notification_from_audit"]

Level = Literal["INFO", "WARN", "CRITICAL"]
LEVEL_RANK: dict[str, int] = {"INFO": 0, "WARN": 1, "CRITICAL": 2}
_BODY_MAX = 3500  # лимит Telegram — 4096 символов на сообщение
_VALUE_MAX = 300

# событие → (уровень, заголовок)
ALERT_RULES: dict[str, tuple[Level, str]] = {
    "CB_TRIPPED": ("CRITICAL", "Circuit breaker TRIPPED: new entries blocked, mode -> PAPER"),
    "KILL_SWITCH": ("CRITICAL", "KILL SWITCH engaged: mode ANALYSIS_ONLY"),
    "RECONCILE_MISMATCH": ("CRITICAL", "Reconciliation mismatch: DB and venue differ"),
    "CB_RESET": ("WARN", "Circuit breaker reset: mode PAPER"),
    "MODE_CHANGED": ("WARN", "Trading mode changed"),
    "ORDER_APPROVAL_REQUIRED": ("WARN", "MICRO_LIVE first order awaits approval"),
    "LLM_BREAKER_OPENED": ("WARN", "LLM breaker OPEN: fallback to ML-only"),
    "LLM_BREAKER_CLOSED": ("INFO", "LLM breaker closed"),
    "ML_MODEL_UNAVAILABLE": ("WARN", "ML model unavailable: Fusion gives NO_SIGNAL"),
    "NEWS_REACTION": ("WARN", "News reaction"),
    "RUN_STARTED": ("INFO", "augur run started"),
    "RUN_STOPPED": ("WARN", "augur run stopped"),
    "RUN_ERROR": ("CRITICAL", "augur run: job failed"),
}
_SEVERITY_LEVEL: dict[str, Level] = {"ERROR": "CRITICAL", "CRITICAL": "CRITICAL"}


class Notification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    level: Level
    kind: str  # событие аудита
    title: str
    body: str
    ref_ids: dict[str, str] = Field(default_factory=dict)

    def text(self) -> str:
        return f"[{self.level}] {self.title}\n{self.body}"[: _BODY_MAX + 200]


def _fmt(v: Any) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True)
    return s if len(s) <= _VALUE_MAX else s[: _VALUE_MAX - 1] + "…"


def notification_from_audit(
    *,
    event: str,
    severity: str,
    actor: str,
    ref_type: str | None,
    ref_id: str | None,
    payload_json: str,
    ts_iso: str,
) -> Notification | None:
    """Алерт по строке аудита или None, если событие алертом не является."""
    rule = ALERT_RULES.get(event)
    level: Level | None
    if rule is not None:
        level, title = rule
    else:
        level, title = _SEVERITY_LEVEL.get(severity), event
    if level is None:
        return None
    try:
        payload = json.loads(payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    if event == "MODE_CHANGED" and isinstance(payload, dict):
        title = f"Trading mode {payload.get('from', '?')} -> {payload.get('to', '?')}"
        if payload.get("to") == "FULL_AUTO":
            level = "CRITICAL"  # реальные деньги без человека в контуре
    if event == "NEWS_REACTION" and severity not in ("WARN", "ERROR", "CRITICAL"):
        return None
    lines = [f"event: {event}", f"time: {ts_iso}", f"actor: {actor}"]
    if ref_type or ref_id:
        lines.append(f"ref: {ref_type or '-'}:{ref_id or '-'}")
    if isinstance(payload, dict):
        lines.extend(f"{k}: {_fmt(v)}" for k, v in sorted(payload.items()))
    body = "\n".join(lines)
    if len(body) > _BODY_MAX:
        body = body[: _BODY_MAX - 1] + "…"
    refs = {"ref_type": ref_type, "ref_id": ref_id}
    return Notification(
        level=level,
        kind=event,
        title=title,
        body=body,
        ref_ids={k: v for k, v in refs.items() if v},
    )
