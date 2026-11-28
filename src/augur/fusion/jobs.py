"""Фоновые задачи Fusion: LLM-контекст, LLM по событию календаря, explain, LLM-breaker,
nightly job ML (FUSION SPEC §3.1, §5.4, §6.2 п. 2 и 8, §6.4; 0.12.0).

Один набор задач для двух хостов:

- in-process Fusion (`FUSION__ENDPOINT` не задан) — их вызывает `augur run`;
- внешний Fusion — их выполняет `augur fusion serve`: ключи `LLM_*_KEY` и ML-артефакты живут в
  процессе Fusion, а не рядом с исполнением (FUSION SPEC §3.6).

LLM никогда не вызывается на критическом пути бара: задачи только обновляют `llm_contexts`,
`decision_audit.explanation` и состояние breaker'ов.
"""

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from augur.backtest.costs import CostModel
from augur.config.settings import Settings
from augur.data.calendar import CalendarError, load_calendar
from augur.db.audit import append_audit

__all__ = ["FusionJobs"]

log = logging.getLogger(__name__)
_ERROR_EVERY_MS = 15 * 60_000
_DAY_MS = 86_400_000


class FusionJobs:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        *,
        clock: Callable[[], int],
        llm_client: Any = None,
        actor: str = "fusion_jobs",
    ) -> None:
        self._conn = conn
        self._s = settings
        self._clock = clock
        self._llm_client = llm_client
        self._actor = actor
        self._handled_events: set[tuple[str, str]] = set()
        self._next: dict[str, int] = {}
        self._last_error: dict[str, int] = {}

    # --- условия --------------------------------------------------------------------

    @property
    def llm_context_enabled(self) -> bool:
        return self._s.fusion.enabled and self._s.fusion.llm.mode == "enabled"

    @property
    def explain_enabled(self) -> bool:
        llm = self._s.fusion.llm
        return llm.mode == "enabled" and llm.explain_enabled

    @property
    def nightly_enabled(self) -> bool:
        return self._s.fusion.enabled and self._s.run.nightly

    # --- задачи ---------------------------------------------------------------------

    async def llm_refresh(self) -> None:
        from augur.fusion.factory import refresh_llm_context

        for symbol in self._s.symbols:
            await refresh_llm_context(
                self._conn,
                self._s,
                symbol=symbol,
                timeframe=self._s.run.timeframe,
                clock=self._clock,
                client=self._llm_client,
            )

    async def llm_pre_event(self) -> list[tuple[str, str]]:
        """Вызов по событию за `macro.llm_pre_event_min` до high-impact события (§3.1).

        Один раз на (символ, событие) за жизнь процесса. Возвращает обработанные пары.
        """
        from augur.fusion.factory import refresh_llm_context

        m = self._s.macro
        if m.llm_pre_event_min <= 0:
            return []
        try:
            cal = load_calendar(m.calendar_path)
        except CalendarError as err:
            log.error("macro calendar unavailable: %s", err)
            return []
        now = self._clock()
        done: list[tuple[str, str]] = []
        for symbol in self._s.symbols:
            events = cal.select(
                start_ms=now,
                end_ms=now + m.llm_pre_event_min * 60_000,
                symbol=symbol,
                aliases=m.currency_aliases,
                impacts=["high"],
            )
            fresh = [ev for ev in events if (symbol, ev.id) not in self._handled_events]
            if not fresh:
                continue
            self._handled_events.update((symbol, ev.id) for ev in fresh)
            await refresh_llm_context(
                self._conn,
                self._s,
                symbol=symbol,
                timeframe=self._s.run.timeframe,
                clock=self._clock,
                event_driven=True,
                client=self._llm_client,
            )
            done.extend((symbol, ev.id) for ev in fresh)
        return done

    async def explain(self) -> None:
        from augur.fusion.factory import build_explain_service

        svc = build_explain_service(self._conn, self._s, clock=self._clock, client=self._llm_client)
        await svc.explain_pending()

    async def llm_breaker(self) -> None:
        from augur.fusion.llm.breaker import sync_breaker_states

        sync_breaker_states(self._conn, self._s.fusion.llm, clock=self._clock)

    def nightly_due(self, symbol: str) -> bool:
        """Сутки UTC: после `run.nightly_hour_utc` и ещё нет `ML_MONITOR_REPORT` за сегодня."""
        now = self._clock()
        day_start = now - now % _DAY_MS
        if datetime.fromtimestamp(now / 1000, tz=UTC).hour < self._s.run.nightly_hour_utc:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM audit_log WHERE event='ML_MONITOR_REPORT' AND ref_type='fusion_asset' "
            "AND ref_id=? AND ts >= ? LIMIT 1",
            (symbol, day_start),
        ).fetchone()
        return row is None

    async def nightly(self, *, force: bool = False, symbols: list[str] | None = None) -> list[Any]:
        """Nightly job ML по символам (`augur.fusion.ml.monitor`). Возвращает отчёты."""
        from augur.fusion.ml.monitor import run_monitor

        ex = self._s.execution
        costs = CostModel(commission_pct=ex.commission_pct, slippage_pct=ex.slippage_pct)
        reports = []
        for symbol in symbols or self._s.symbols:
            if not force and not self.nightly_due(symbol):
                continue
            reports.append(
                run_monitor(
                    self._conn,
                    self._s.fusion.ml,
                    asset=symbol,
                    timeframe=self._s.run.timeframe,
                    clock=self._clock,
                    min_confidence=self._s.fusion.config.ml_min_confidence,
                    cost_rate=costs.commission_rate + costs.slippage_rate,
                )
            )
        return reports

    # --- планировщик для `augur fusion serve` ----------------------------------------

    async def tick(self) -> None:
        """Один проход планировщика (интервалы — `run.*`). Сбой задачи не останавливает цикл."""
        run = self._s.run
        if self.llm_context_enabled:
            await self._every("llm_refresh", run.llm_refresh_s, self.llm_refresh)
            await self._safe("llm_pre_event", self.llm_pre_event)
        if self.explain_enabled:
            await self._every("explain", run.explain_s, self.explain)
        await self._safe("llm_breaker", self.llm_breaker)
        if self.nightly_enabled:
            await self._safe("nightly", self.nightly)

    async def _every(self, name: str, interval_s: float, job: Callable[[], Awaitable[Any]]) -> None:
        now = self._clock()
        if now < self._next.get(name, 0):
            return
        self._next[name] = now + int(interval_s * 1000)
        await self._safe(name, job)

    async def _safe(self, name: str, job: Callable[[], Awaitable[Any]]) -> None:
        try:
            await job()
        except Exception as e:  # noqa: BLE001 — сбой задачи не останавливает сервис
            log.exception("fusion job %s failed", name)
            now = self._clock()
            if now - self._last_error.get(name, -_ERROR_EVERY_MS) < _ERROR_EVERY_MS:
                return
            self._last_error[name] = now
            try:
                self._conn.rollback()
                append_audit(
                    self._conn,
                    actor=self._actor,
                    event="RUN_ERROR",
                    severity="CRITICAL",
                    payload={"job": name, "error": type(e).__name__},
                    ts=now,
                )
                self._conn.commit()
            except sqlite3.Error:
                log.exception("cannot write RUN_ERROR")
