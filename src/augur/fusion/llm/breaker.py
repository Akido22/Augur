"""Переходы LLM-breaker в `audit_log` (ML_LLM_FUSION_SPEC.md §5.4; 0.11.0).

Сам breaker — функция от `llm_calls` (доля отклонённых за час > `breaker_reject_ratio` при
≥ `breaker_min_calls` вызовах; `breaker_stats`). Здесь фиксируются только смены состояния:
`LLM_BREAKER_OPENED` (WARN) и `LLM_BREAKER_CLOSED` (INFO) по задаче (`ref_id` = task) — по
ним уходят алерты. Предыдущее состояние — последнее такое событие в аудите.

Отличие от §5.4: состояния HALF_OPEN с пробными вызовами нет — breaker закрывается, когда
отклонённые вызовы выходят из часового окна (поведение 0.10.0 не меняется).
"""

import sqlite3
from collections.abc import Callable
from typing import get_args

from augur.config.settings import LLMSettings
from augur.db.audit import append_audit
from augur.fusion.llm.calls import Task, breaker_stats

__all__ = ["BREAKER_TASKS", "breaker_state", "sync_breaker_states"]

BREAKER_TASKS: tuple[Task, ...] = get_args(Task)
OPENED, CLOSED = "LLM_BREAKER_OPENED", "LLM_BREAKER_CLOSED"


def breaker_state(conn: sqlite3.Connection, task: str) -> bool:
    """Последнее записанное состояние: True — OPEN."""
    row = conn.execute(
        "SELECT event FROM audit_log WHERE event IN (?, ?) AND ref_type = 'llm_task' "
        "AND ref_id = ? ORDER BY id DESC LIMIT 1",
        (OPENED, CLOSED, task),
    ).fetchone()
    return row is not None and row[0] == OPENED


def sync_breaker_states(
    conn: sqlite3.Connection, cfg: LLMSettings, *, clock: Callable[[], int]
) -> list[tuple[str, bool]]:
    """Записать смены состояния breaker по всем задачам. Возвращает [(task, open)] изменений."""
    now = clock()
    changed: list[tuple[str, bool]] = []
    try:
        for task in BREAKER_TASKS:
            is_open, total, bad = breaker_stats(conn, cfg, task, now=now)
            if is_open == breaker_state(conn, task):
                continue
            append_audit(
                conn,
                actor="llm_breaker",
                event=OPENED if is_open else CLOSED,
                severity="WARN" if is_open else "INFO",
                ref_type="llm_task",
                ref_id=task,
                payload={
                    "task": task,
                    "calls_1h": total,
                    "rejected_1h": bad,
                    "reject_ratio_limit": cfg.breaker_reject_ratio,
                    "min_calls": cfg.breaker_min_calls,
                },
                ts=now,
            )
            changed.append((task, is_open))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return changed
