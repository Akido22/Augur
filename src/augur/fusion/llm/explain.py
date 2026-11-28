"""Асинхронный LLM `explain` → `decision_audit.explanation` (FUSION SPEC §3.1, §4.4, §6.2 п. 8).

- Только для решений по итоговому сигналу ENTER/EXIT/REDUCE (`BUY`/`SELL`/`CLOSE`/`REDUCE`),
  включая отклонённые Risk Gate; системные выходы (`strategy_version = system:*`) и HOLD /
  NO_SIGNAL не объясняются — экономия бюджета.
- Вызывается после решения (оркестратор, `augur llm explain`), не на критическом пути.
  Выход никогда не возвращается в Fusion и Risk Gate: он только записывается один раз
  (`UPDATE … WHERE explanation IS NULL`; триггер `decision_audit_immutable` запрещает
  перезапись).
- Вход — whitelist полей решения, сигнала и `fused_signal` (без `client_order_id`, баланса и
  ключей), очищенный `clean_text`; перед отправкой — сканер секретов (`CallRunner`).
- Схема `SignalExplanation` (strict, extra=forbid), `decision_id` сверяется со входом.
  Невалидный ответ — один повтор с текстом ошибки (`llm.max_retries`), без «починки»; затем
  объяснения нет. Каждая попытка — строка `llm_calls` (task `explain`,
  `request_group = decision_audit.id`): по ней решение больше не берётся в работу.
- Бюджет: explain — не событийный вызов, при ≥ 80 % дневного бюджета не выполняется; при
  открытом LLM-breaker задачи `explain` — тоже.
"""

import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from augur.config.settings import LLMSettings
from augur.fusion.llm.calls import CallLog, CallRunner
from augur.fusion.llm.client import LLMClient
from augur.fusion.llm.prompts import PromptTemplate, PromptVersionConflict
from augur.fusion.llm.sanitize import clean_text
from augur.fusion.llm.schema import SignalExplanation

__all__ = ["EXPLAIN_ACTIONS", "EXPLAIN_PROMPT", "ExplainResult", "ExplainService"]

EXPLAIN_PROMPT = "prompt_explain_v1"
EXPLAIN_ACTIONS = ("BUY", "SELL", "CLOSE", "REDUCE")
log = logging.getLogger(__name__)
_HOUR_MS = 3_600_000
_REPAIR_NOTE = (
    "\n\nПредыдущий ответ отклонён проверкой схемы: {error}\n"
    "Ответь заново только JSON-объектом строго по схеме."
)


class ExplainResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str
    status: str  # ok | rejected | skipped:<reason> | unavailable
    llm_call_ids: list[int] = []


def _strip_ids(obj: Any) -> Any:
    """Убрать из source_breakdown служебные id (экономия токенов, ничего не объясняют)."""
    if isinstance(obj, dict):
        return {
            k: _strip_ids(v)
            for k, v in obj.items()
            if not (k.endswith("_id") or k.endswith("_ids") or k == "id")
        }
    if isinstance(obj, list):
        return [_strip_ids(v) for v in obj]
    return obj


class ExplainService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: LLMSettings,
        client: LLMClient,
        keys: Mapping[str, SecretStr | None],
        *,
        clock: Callable[[], int],
    ) -> None:
        self._conn = conn
        self._cfg = cfg
        self._runner = CallRunner(conn, cfg, client, keys, clock=clock)
        self._prompt = PromptTemplate("explain", EXPLAIN_PROMPT)
        self._clock = clock

    def pending(self, limit: int) -> list[str]:
        """id решений, ждущих объяснения (не старше `explain_max_age_h`, ещё не пробованных)."""
        since = self._clock() - self._cfg.explain_max_age_h * _HOUR_MS
        rows = self._conn.execute(
            "SELECT d.id FROM decision_audit d JOIN signals s ON s.id = d.signal_id "
            "WHERE d.explanation IS NULL AND s.action IN ('BUY', 'SELL', 'CLOSE', 'REDUCE') "
            "AND d.strategy_version NOT LIKE 'system:%' AND d.created_at >= ? "
            "AND NOT EXISTS (SELECT 1 FROM llm_calls c WHERE c.task = 'explain' "
            "AND c.request_group = d.id) ORDER BY d.created_at, d.id LIMIT ?",
            (since, limit),
        ).fetchall()
        return [str(r[0]) for r in rows]

    async def explain_pending(self, limit: int = 5) -> list[ExplainResult]:
        if self._cfg.mode != "enabled" or not self._cfg.explain_enabled:
            return []
        return [await self.explain(d) for d in self.pending(limit)]

    def _inputs(self, decision_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT d.risk_verdict, d.risk_reason, d.mode, d.strategy_version, "
            "d.ml_model_version, d.llm_prompt_version, d.llm_status, s.symbol, s.timeframe, "
            "s.action, s.strength, s.source, s.risk_flags_json, f.reason, "
            "f.source_breakdown_json FROM decision_audit d JOIN signals s ON s.id = d.signal_id "
            "LEFT JOIN fused_signals f ON f.id = d.fused_signal_id WHERE d.id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            return None
        breakdown = ""
        if row[14]:
            try:
                breakdown = json.dumps(
                    _strip_ids(json.loads(row[14])), ensure_ascii=False, sort_keys=True
                )
            except json.JSONDecodeError:
                breakdown = ""
        return {
            "decision_id": decision_id,
            "risk_verdict": row[0],
            "risk_reason": clean_text(row[1], 400),
            "mode": row[2],
            "strategy_version": clean_text(row[3], 80),
            "ml_model_version": clean_text(row[4], 60),
            "llm_prompt_version": clean_text(row[5], 60),
            "llm_status": row[6] or "-",
            "symbol": clean_text(row[7], 30),
            "timeframe": row[8] or "-",
            "action": row[9],
            "strength": f"{float(row[10]):.3f}",
            "source": row[11],
            "risk_flags": clean_text(row[12], 400),
            "fusion_reason": clean_text(row[13], 200) if row[13] else "-",
            "breakdown": clean_text(breakdown, 1500),
        }

    async def explain(self, decision_id: str) -> ExplainResult:
        values = self._inputs(decision_id)
        if values is None:
            return ExplainResult(decision_id=decision_id, status="skipped:not_found")
        try:
            self._prompt.ensure_registered(self._conn, self._cfg, 0.0, self._clock())
        except PromptVersionConflict as e:
            log.error("%s", e)
            return ExplainResult(decision_id=decision_id, status="skipped:prompt_conflict")
        if self._runner.breaker_open("explain"):
            return ExplainResult(decision_id=decision_id, status="skipped:llm_breaker_open")
        blocked = self._runner.budget_block(asset=None, event_driven=False)
        if blocked is not None:
            # без строки llm_calls: решение вернётся в очередь, когда бюджет освободится
            return ExplainResult(decision_id=decision_id, status=f"skipped:{blocked}")
        system, user = self._prompt.render(**values)
        meta = CallLog(
            self._prompt.prompt_hash(system, user),
            "explain",
            EXPLAIN_PROMPT,
            values["symbol"],
            decision_id,
            0,
            0.0,
        )
        call_ids: list[int] = []
        prompt_user = user
        for _attempt in range(1 + self._cfg.max_retries):
            att = await self._runner.call(meta, system=system, user=prompt_user)
            if att is None:
                return ExplainResult(
                    decision_id=decision_id, status="unavailable", llm_call_ids=call_ids
                )
            call_ids.append(att.call_id)
            try:
                out = SignalExplanation.model_validate_json(
                    att.text, context={"decision_id": decision_id}
                )
            except ValidationError as e:
                msg = str(e)[:500]
                self._runner.mark(
                    att.call_id, "ungrounded" if "ungrounded" in msg else "invalid_schema", msg
                )
                prompt_user = user + _REPAIR_NOTE.format(error=msg)
                continue
            self._store(decision_id, out, att.call_id, att.provider)
            return ExplainResult(decision_id=decision_id, status="ok", llm_call_ids=call_ids)
        return ExplainResult(decision_id=decision_id, status="rejected", llm_call_ids=call_ids)

    def _store(self, decision_id: str, out: SignalExplanation, call_id: int, provider: str) -> None:
        doc = {
            "prompt_version": EXPLAIN_PROMPT,
            "llm_call_id": call_id,
            "provider": provider,
            "summary": out.summary,
            "key_factors": out.key_factors,
            "caveats": out.caveats,
        }
        try:
            self._conn.execute(
                "UPDATE decision_audit SET explanation = ? WHERE id = ? AND explanation IS NULL",
                (json.dumps(doc, ensure_ascii=False, sort_keys=True), decision_id),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
