"""Выполнение одного LLM-запроса: бюджет, сканер секретов, цепочка провайдеров, `llm_calls`.

FUSION SPEC §3.1 (бюджет), §3.4 (fallback провайдеров), §3.5 (каждая попытка — строка
`llm_calls`), §3.6 (сканер секретов), §5.4 (LLM-breaker по доле отклонённых за час).
"""

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import SecretStr

from augur.config.settings import LLMProvider, LLMSettings
from augur.fusion.llm.client import LLMClient, LLMHTTPError
from augur.fusion.llm.sanitize import find_secret

__all__ = ["Attempt", "CallLog", "CallRunner", "Task", "breaker_stats"]

Task = Literal["context", "explain", "news_sentiment"]
_HOUR_MS = 3_600_000
_DAY_MS = 86_400_000


def breaker_stats(
    conn: sqlite3.Connection, cfg: LLMSettings, task: Task, *, now: int
) -> tuple[bool, int, int]:
    """(открыт ли LLM-breaker, вызовов за час, отклонённых за час) по `llm_calls` (§5.4)."""
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(status NOT IN ('ok','budget_block')), 0) FROM llm_calls "
        "WHERE task=? AND created_at >= ? AND status <> 'budget_block'",
        (task, now - _HOUR_MS),
    ).fetchone()
    total, bad = int(row[0]), int(row[1])
    return total >= cfg.breaker_min_calls and bad / total > cfg.breaker_reject_ratio, total, bad


@dataclass(frozen=True)
class Attempt:
    """Успешный ответ провайдера (ещё не провалидированный) и id его строки в `llm_calls`."""

    call_id: int
    text: str
    provider: str


@dataclass(frozen=True)
class CallLog:
    prompt_hash: str
    task: Task
    template_id: str
    asset: str | None
    request_group: str
    sample_index: int
    temperature: float | None


class CallRunner:
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
        self._client = client
        self._keys = dict(keys)
        self._clock = clock

    # --- журнал --------------------------------------------------------------------

    def log(
        self,
        meta: CallLog,
        *,
        provider: str,
        model: str,
        status: str,
        latency_ms: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        error: str | None = None,
        response: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO llm_calls(prompt_hash, provider, model, input_tokens, output_tokens, "
            "latency_ms, cost_usd, created_at, task, template_id, asset, request_group, "
            "sample_index, temperature, status, error, response_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                meta.prompt_hash,
                provider,
                model,
                input_tokens,
                output_tokens,
                latency_ms,
                cost_usd,
                self._clock(),
                meta.task,
                meta.template_id,
                meta.asset,
                meta.request_group,
                meta.sample_index,
                meta.temperature,
                status,
                error,
                response,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def mark(self, call_id: int, status: str, error: str | None) -> None:
        """Итог валидации ответа (ok → invalid_schema | ungrounded) для этой строки."""
        self._conn.execute(
            "UPDATE llm_calls SET status=?, error=? WHERE id=?", (status, error, call_id)
        )
        self._conn.commit()

    # --- ограничения ---------------------------------------------------------------

    def spent_today(self) -> float:
        now = self._clock()
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE created_at >= ?",
            (now - now % _DAY_MS,),
        ).fetchone()
        return float(row[0])

    def budget_block(self, *, asset: str | None, event_driven: bool) -> str | None:
        """Причина отказа без сетевого вызова или None (§3.1)."""
        if self._cfg.daily_usd > 0:
            spent = self.spent_today()
            if spent >= self._cfg.daily_usd:
                return "budget_exceeded"
            if (
                not event_driven
                and spent >= self._cfg.daily_usd * self._cfg.event_only_budget_fraction
            ):
                return "budget_event_only"
        if asset is not None:
            row = self._conn.execute(
                "SELECT COUNT(DISTINCT request_group) FROM llm_calls WHERE asset=? "
                "AND created_at >= ? AND status <> 'budget_block'",
                (asset, self._clock() - _HOUR_MS),
            ).fetchone()
            if int(row[0]) >= self._cfg.max_calls_per_asset_hour:
                return "asset_hourly_limit"
        return None

    def breaker_open(self, task: Task) -> bool:
        """LLM-breaker (§5.4): доля отклонённых за час > порога при ≥ min_calls вызовов."""
        return breaker_stats(self._conn, self._cfg, task, now=self._clock())[0]

    def known_secrets(self) -> list[str]:
        return [k.get_secret_value() for k in self._keys.values() if k is not None]

    # --- вызов ---------------------------------------------------------------------

    async def call(self, meta: CallLog, *, system: str, user: str) -> Attempt | None:
        """Первый ответивший провайдер; сбой → следующий по списку. None — все упали."""
        secret = find_secret(system + "\n" + user, self.known_secrets())
        if secret is not None:
            self.log(meta, provider="-", model="-", status="secret_block", error=secret)
            return None
        for provider in self._cfg.providers:
            attempt = await self._call_one(provider, meta, system=system, user=user)
            if attempt is not None:
                return attempt
        return None

    async def _call_one(
        self, provider: LLMProvider, meta: CallLog, *, system: str, user: str
    ) -> Attempt | None:
        key = self._keys.get(provider.api_key) if provider.api_key else None
        if provider.api_key and key is None:
            self.log(
                meta,
                provider=provider.name,
                model=provider.model,
                status="http_error",
                error="api key not configured",
            )
            return None
        try:
            raw = await self._client.complete_json(
                provider,
                api_key=key,
                system=system,
                user=user,
                temperature=meta.temperature,
                timeout_s=self._cfg.timeout_s,
                max_tokens=self._cfg.max_tokens,
            )
        except LLMHTTPError as e:
            self.log(
                meta,
                provider=provider.name,
                model=provider.model,
                status="timeout" if e.kind == "timeout" else "http_error",
                latency_ms=e.latency_ms,
                error=f"{e.kind}: {e}",
            )
            return None
        price = provider.price_per_mtok
        cost = (raw.input_tokens * price.input + raw.output_tokens * price.output) / 1_000_000
        call_id = self.log(
            meta,
            provider=provider.name,
            model=provider.model,
            status="ok",
            latency_ms=raw.latency_ms,
            input_tokens=raw.input_tokens,
            output_tokens=raw.output_tokens,
            cost_usd=round(cost, 8),
            response=raw.text,
        )
        return Attempt(call_id=call_id, text=raw.text, provider=provider.name)
