"""Запись вердикта Risk Gate в `decision_audit` (VERSIONING.md §6.1–6.2, ARCH §7.2, §7.5).

Пишется КАЖДЫЙ вердикт, включая REJECTED и неисполняемые HOLD/NO_SIGNAL (FUSION SPEC §5.5).
Одна транзакция: `decision_audit` INSERT + `signals.status` + `audit_log` (`RISK_APPROVED` /
`RISK_REJECTED` с полным списком `CheckResult`). Ошибка → ROLLBACK, исключение наверх:
нет записи в аудите — нет ордера.

Версии ML/LLM: пока артефакт не участвовал в решении (до 0.10.0) — `"n/a"` (VERSIONING.md §2.3).
"""

import json
import sqlite3
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from augur.db.audit import append_audit
from augur.ids import new_ulid
from augur.risk.models import RiskVerdict, VerdictKind
from augur.signals.ready import SignalReady, decision_id_from_ref

__all__ = [
    "AUDIT_ACTOR",
    "FORBIDDEN_VALUES",
    "NO_ARTIFACT",
    "REQUIRED_VERSION_FIELDS",
    "DecisionAuditRecord",
    "DecisionAuditWriter",
    "UnauditableOrderError",
    "assert_auditable",
]

AUDIT_ACTOR = "risk_gate"
NO_ARTIFACT = "n/a"  # VERSIONING.md §2.3: артефакт (ML-модель / LLM-промпт) не участвовал в решении

REQUIRED_VERSION_FIELDS = (
    "app_version",
    "strategy_version",
    "ml_model_version",
    "llm_prompt_version",
)
FORBIDDEN_VALUES = frozenset({"", "latest", "current", "dev", "unknown"})


class UnauditableOrderError(RuntimeError):
    """Запись аудита неполна: решение не может быть исполнено (VERSIONING.md §6.2)."""


class DecisionAuditRecord(BaseModel):
    """Строка `decision_audit` (VERSIONING.md §6.1) без write-once полей."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    signal_id: str
    fused_signal_id: str | None
    app_version: str
    strategy_version: str
    ml_model_version: str
    llm_prompt_version: str
    ml_prediction_id: str | None
    llm_status: Literal["ok", "rejected", "fallback_ml_only"] | None
    risk_verdict: VerdictKind
    risk_reason: str | None
    config_hash: str
    mode: str
    created_at: int  # Unix ms UTC


def assert_auditable(rec: DecisionAuditRecord) -> None:
    """VERSIONING.md §6.2: все поля версий заполнены и не «плавающие».

    Raises:
        UnauditableOrderError: пустое/запрещённое значение или `app_version` `+unknown`.
    """
    bad = [
        f for f in REQUIRED_VERSION_FIELDS if getattr(rec, f).strip().lower() in FORBIDDEN_VALUES
    ]
    if rec.app_version.endswith("+unknown"):
        bad.append("app_version")
    if not rec.config_hash.strip():
        bad.append("config_hash")
    if bad:
        raise UnauditableOrderError(f"missing/invalid versions: {sorted(set(bad))}")


class DecisionAuditWriter:
    """Записывает вердикты Risk Gate.

    Args:
        conn: соединение с применёнными миграциями.
        app_version: `augur.__version__` (с `+unknown` запись отклоняется).
        config_hash: `config_hash(settings)`.
    """

    def __init__(self, conn: sqlite3.Connection, *, app_version: str, config_hash: str) -> None:
        self._conn = conn
        self._app_version = app_version
        self._config_hash = config_hash

    def strategy_version_for(self, strategy_signal_ids: Sequence[str]) -> str:
        """`strategy_version` родительских сигналов (`signals.strategy`), через запятую."""
        if not strategy_signal_ids:
            return ""
        marks = ",".join("?" * len(strategy_signal_ids))
        rows = self._conn.execute(
            f"SELECT DISTINCT strategy FROM signals WHERE kind='strategy' AND id IN ({marks})",  # noqa: S608 — только плейсхолдеры
            list(strategy_signal_ids),
        ).fetchall()
        return ",".join(sorted(str(r[0]) for r in rows if r[0]))

    def build_record(
        self,
        sig: SignalReady,
        verdict: RiskVerdict,
        *,
        strategy_version: str | None = None,
        ml_model_version: str = NO_ARTIFACT,
        llm_prompt_version: str = NO_ARTIFACT,
        ml_prediction_id: str | None = None,
        llm_status: Literal["ok", "rejected", "fallback_ml_only"] | None = None,
    ) -> DecisionAuditRecord:
        """Собирает запись; `id` — ULID из `explanation_ref="decision_audit:<ulid>"` или новый."""
        if verdict.signal_id != sig.signal_id:
            raise ValueError("verdict does not belong to signal")
        created_at = int(verdict.evaluated_at.timestamp() * 1000)
        return DecisionAuditRecord(
            id=decision_id_from_ref(sig.explanation_ref) or new_ulid(created_at),
            signal_id=sig.signal_id,
            fused_signal_id=sig.fused_signal_id,
            app_version=self._app_version,
            strategy_version=(
                strategy_version
                if strategy_version is not None
                else self.strategy_version_for(sig.strategy_signal_ids)
            ),
            ml_model_version=ml_model_version,
            llm_prompt_version=llm_prompt_version,
            ml_prediction_id=ml_prediction_id,
            llm_status=llm_status,
            risk_verdict=verdict.risk_verdict,
            risk_reason=verdict.risk_reason,
            config_hash=self._config_hash,
            mode=verdict.mode.value,
            created_at=created_at,
        )

    def record(
        self,
        sig: SignalReady,
        verdict: RiskVerdict,
        *,
        strategy_version: str | None = None,
        ml_model_version: str = NO_ARTIFACT,
        llm_prompt_version: str = NO_ARTIFACT,
        ml_prediction_id: str | None = None,
        llm_status: Literal["ok", "rejected", "fallback_ml_only"] | None = None,
    ) -> DecisionAuditRecord:
        """Атомарно пишет вердикт. Возвращает записанную строку.

        Raises:
            UnauditableOrderError: неполные версии — ничего не записано.
            sqlite3.Error: ошибка БД — транзакция откатана.
        """
        rec = self.build_record(
            sig,
            verdict,
            strategy_version=strategy_version,
            ml_model_version=ml_model_version,
            llm_prompt_version=llm_prompt_version,
            ml_prediction_id=ml_prediction_id,
            llm_status=llm_status,
        )
        assert_auditable(rec)
        event = "RISK_REJECTED" if verdict.risk_verdict == "REJECTED" else "RISK_APPROVED"
        status = "REJECTED" if verdict.risk_verdict == "REJECTED" else "APPROVED"
        try:
            self._conn.execute(
                "INSERT INTO decision_audit(id, signal_id, fused_signal_id, app_version, "
                "strategy_version, ml_model_version, llm_prompt_version, ml_prediction_id, "
                "llm_status, risk_verdict, risk_reason, config_hash, mode, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rec.id,
                    rec.signal_id,
                    rec.fused_signal_id,
                    rec.app_version,
                    rec.strategy_version,
                    rec.ml_model_version,
                    rec.llm_prompt_version,
                    rec.ml_prediction_id,
                    rec.llm_status,
                    rec.risk_verdict,
                    rec.risk_reason,
                    rec.config_hash,
                    rec.mode,
                    rec.created_at,
                ),
            )
            cur = self._conn.execute(
                "UPDATE signals SET status = ? WHERE id = ? AND kind = 'final'",
                (status, sig.signal_id),
            )
            if cur.rowcount != 1:
                raise UnauditableOrderError(f"final signal not found: {sig.signal_id}")
            append_audit(
                self._conn,
                actor=AUDIT_ACTOR,
                event=event,
                severity="INFO" if event == "RISK_APPROVED" else "WARN",
                ref_type="signal",
                ref_id=sig.signal_id,
                payload={
                    "decision_audit_id": rec.id,
                    "verdict": json.loads(verdict.model_dump_json()),
                },
                ts=rec.created_at,
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return rec
