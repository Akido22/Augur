"""`LLMContextService` (ML_LLM_FUSION_SPEC.md §3.4, §5; skill llm-output-validation).

`refresh()` — асинхронное обновление контекста актива (по расписанию или событию, вне
критического пути бара, §6.2 п. 2): N сэмплов с разным порядком новостей → валидация →
variance filter → строка `llm_contexts`. `latest()` — последний контекст для Fusion.
Любой сбой даёт `fallback_ml_only` / `rejected` с причиной и WARNING `LLM_FALLBACK` в `audit_log`;
исключения наружу не выходят, повторов «до успеха» нет.
"""

import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from pydantic import SecretStr

from augur.config.settings import LLMSettings
from augur.db.audit import append_audit
from augur.fusion.llm.calls import CallLog, CallRunner
from augur.fusion.llm.client import LLMClient
from augur.fusion.llm.inputs import EventInput, NewsInput, PriceSummary
from augur.fusion.llm.prompts import PromptTemplate, PromptVersionConflict
from augur.fusion.llm.schema import LLMContext, LLMContextResult
from augur.fusion.llm.validate import aggregate_samples, parse_context
from augur.ids import new_ulid

__all__ = ["CONTEXT_PROMPT", "LLMContextService", "load_context"]

CONTEXT_PROMPT = "prompt_context_v1"
AUDIT_ACTOR = "llm_context"
log = logging.getLogger(__name__)

_REPAIR_NOTE = (
    "\n\nПредыдущий ответ отклонён проверкой схемы: {error}\n"
    "Ответь заново только JSON-объектом строго по схеме."
)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


class LLMContextService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: LLMSettings,
        client: LLMClient,
        keys: Mapping[str, SecretStr | None],
        *,
        clock: Callable[[], int],
        prompt_version: str = CONTEXT_PROMPT,
    ) -> None:
        self._conn = conn
        self._cfg = cfg
        self._clock = clock
        self._runner = CallRunner(conn, cfg, client, keys, clock=clock)
        self._prompt = PromptTemplate("context", prompt_version)
        self.prompt_version = prompt_version

    # --- чтение для Fusion -------------------------------------------------------------

    def latest(self, asset: str, *, now_ms: int, max_age_s: int) -> LLMContextResult | None:
        """Последний результат по активу не старше `max_age_s` (as_of ≤ now)."""
        r = self._conn.execute(
            "SELECT id, status, reject_reason, prompt_version, context_json, llm_call_ids_json "
            "FROM llm_contexts WHERE asset=? AND as_of <= ? AND as_of >= ? "
            "ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1",
            (asset, now_ms, now_ms - max_age_s * 1000),
        ).fetchone()
        return _result_from_row(r) if r else None

    # --- обновление --------------------------------------------------------------------

    async def refresh(
        self,
        asset: str,
        *,
        as_of: int,
        news: list[NewsInput],
        price: PriceSummary,
        event_driven: bool = False,
        events: list[EventInput] | None = None,
        imminent_ids: set[str] | None = None,
    ) -> LLMContextResult:
        """`events` — события календаря (0.12.0): их id — evidence; `imminent_ids` — события,
        для которых допустим `high_impact_event_imminent` (FUSION SPEC §5.1)."""
        events = list(events or [])
        imminent = set(imminent_ids or set()) & {e.event_id for e in events}
        group = new_ulid(self._clock())
        if self._cfg.mode != "enabled":
            return self._finish(asset, as_of, group, "fallback_ml_only", None, [], "llm_disabled")
        temperature = self._cfg.variance.temperature
        try:
            self._prompt.ensure_registered(self._conn, self._cfg, temperature, self._clock())
        except PromptVersionConflict as e:
            log.error("%s", e)
            return self._finish(
                asset, as_of, group, "fallback_ml_only", None, [], "prompt_conflict"
            )
        if self._runner.breaker_open("context"):
            return self._finish(
                asset, as_of, group, "fallback_ml_only", None, [], "llm_breaker_open"
            )
        blocked = self._runner.budget_block(asset=asset, event_driven=event_driven)
        if blocked is not None:
            meta = CallLog("-", "context", self.prompt_version, asset, group, 0, None)
            cid = self._runner.log(
                meta, provider="-", model="-", status="budget_block", error=blocked
            )
            return self._finish(asset, as_of, group, "fallback_ml_only", None, [cid], blocked)

        as_of_iso = _iso(as_of)
        as_of_dt = datetime.fromtimestamp(as_of / 1000, tz=UTC)
        input_ids = {n.news_id for n in news} | {e.event_id for e in events}
        # Экономия (§5.2): без новостей и high-impact события — один сэмпл; иначе N сэмплов
        # с разным порядком новостей («3 сэмпла — всегда при high-impact событии»).
        n_samples = self._cfg.variance.samples if news or imminent else 1
        call_ids: list[int] = []
        valid: list[LLMContext] = []
        for i in range(n_samples):
            order = news[i % len(news) :] + news[: i % len(news)] if news else []
            system, user = self._prompt.render(
                asset=asset,
                as_of_iso=as_of_iso,
                news=[{**n.model_dump(), "published_iso": n.published_iso} for n in order],
                events=[e.model_dump() for e in events],
                window_h=price.window_h,
                ret=price.ret,
                vol_z=price.vol_z,
            )
            meta = CallLog(
                self._prompt.prompt_hash(system, user),
                "context",
                self.prompt_version,
                asset,
                group,
                i,
                temperature if n_samples > 1 else 0.0,
            )
            ctx = await self._sample(
                meta, system, user, asset, as_of_dt, input_ids, imminent, call_ids
            )
            if ctx is not None:
                valid.append(ctx)
        if (
            not call_ids
            or not valid
            and all(
                s in ("timeout", "http_error", "secret_block") for s in self._statuses(call_ids)
            )
        ):
            return self._finish(
                asset, as_of, group, "fallback_ml_only", None, call_ids, "providers_unavailable"
            )
        agg, reason = aggregate_samples(valid, self._cfg.variance, requested=n_samples)
        if agg is None:
            return self._finish(asset, as_of, group, "rejected", None, call_ids, reason)
        return self._finish(asset, as_of, group, "ok", agg, call_ids, None)

    async def _sample(
        self,
        meta: CallLog,
        system: str,
        user: str,
        asset: str,
        as_of: datetime,
        input_ids: set[str],
        imminent: set[str],
        call_ids: list[int],
    ) -> LLMContext | None:
        prompt_user = user
        for _attempt in range(1 + self._cfg.max_retries):
            att = await self._runner.call(meta, system=system, user=prompt_user)
            if att is None:
                call_ids.extend(self._group_ids(meta))
                return None
            call_ids.append(att.call_id)
            ctx, status, error = parse_context(
                att.text,
                asset=asset,
                as_of=as_of,
                input_ids=input_ids,
                event_ids=imminent,
                has_inputs=bool(input_ids),
                cfg=self._cfg,
            )
            if ctx is not None:
                return ctx
            self._runner.mark(att.call_id, status, error)
            # Повтор: модели возвращается текст ошибки без исходного ответа (§5.1).
            prompt_user = user + _REPAIR_NOTE.format(error=error)
        return None

    def _group_ids(self, meta: CallLog) -> list[int]:
        rows = self._conn.execute(
            "SELECT id FROM llm_calls WHERE request_group=? AND sample_index=? ORDER BY id",
            (meta.request_group, meta.sample_index),
        ).fetchall()
        return [int(r[0]) for r in rows]

    def _statuses(self, ids: list[int]) -> list[str]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT status FROM llm_calls WHERE id IN ({marks})",  # noqa: S608 — плейсхолдеры
            ids,
        ).fetchall()
        return [str(r[0]) for r in rows]

    def _finish(
        self,
        asset: str,
        as_of: int,
        group: str,
        status: str,
        ctx: LLMContext | None,
        call_ids: list[int],
        reason: str | None,
    ) -> LLMContextResult:
        ids = sorted(set(call_ids))
        now = self._clock()
        cid = new_ulid(now)
        self._conn.execute(
            "INSERT INTO llm_contexts(id, asset, as_of, status, reject_reason, prompt_version, "
            "context_json, llm_call_ids_json, request_group, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                cid,
                asset,
                as_of,
                status,
                reason,
                self.prompt_version,
                ctx.model_dump_json() if ctx is not None else None,
                json.dumps(ids),
                group,
                now,
            ),
        )
        if status != "ok":
            log.warning("LLM fallback asset=%s status=%s reason=%s", asset, status, reason)
            append_audit(
                self._conn,
                actor=AUDIT_ACTOR,
                event="LLM_FALLBACK",
                severity="WARN",
                ref_type="llm_context",
                ref_id=cid,
                payload={"asset": asset, "status": status, "reason": reason, "llm_call_ids": ids},
                ts=now,
            )
        self._conn.commit()
        return LLMContextResult(
            status=status,  # type: ignore[arg-type]
            context=ctx,
            llm_call_ids=ids,
            reject_reason=reason,
            prompt_version=self.prompt_version,
            context_id=cid,
        )


def _result_from_row(r: tuple[object, ...]) -> LLMContextResult:
    ctx_json = r[4]
    ctx = (
        LLMContext.model_validate_json(
            str(ctx_json), context={"input_ids": _evidence(str(ctx_json))}
        )
        if ctx_json
        else None
    )
    return LLMContextResult(
        status=r[1],  # type: ignore[arg-type]
        context=ctx,
        llm_call_ids=json.loads(str(r[5])),
        reject_reason=r[2],  # type: ignore[arg-type]
        prompt_version=str(r[3]),
        context_id=str(r[0]),
    )


def _evidence(ctx_json: str) -> set[str]:
    data = json.loads(ctx_json)
    return {e for f in data.get("risk_flags", []) for e in f.get("evidence_ids", [])}


def load_context(conn: sqlite3.Connection, context_id: str) -> LLMContextResult | None:
    """Строка `llm_contexts` по id (для replay, FUSION SPEC §5.5)."""
    r = conn.execute(
        "SELECT id, status, reject_reason, prompt_version, context_json, llm_call_ids_json "
        "FROM llm_contexts WHERE id=?",
        (context_id,),
    ).fetchone()
    return _result_from_row(r) if r else None
