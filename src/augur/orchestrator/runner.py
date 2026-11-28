"""Минимальный оркестратор `augur run` (ARCHITECTURE.md §2.12; 0.11.0).

Последовательный asyncio-цикл без EventBus и DbWriter: одно соединение SQLite, задачи
выполняются по очереди в одном процессе, поэтому порядок решений по символу детерминирован.
Каждый тик (`run.tick_s`):

1. Выбор стеков исполнения по `mode_state`: PAPER-стек (PaperAdapter с котировками
   data-адаптера) всегда; live-стек — если конфиг разрешает MICRO_LIVE и текущий режим live
   или в live-книге остались позиции/ордера (`live_book_mode`). Смена режима (`augur mode set`
   из CLI, CB, kill switch) подхватывается без рестарта: live-стек пересобирается под книгу.
2. Закрытие бара (`run.timeframe`, + `bar_close_delay_s`): бары с data-адаптера → `ohlcv`;
   стратегии (`run.strategies`) на последнем закрытом баре → `SignalEngine.resolve_signal`
   (Fusion) → `TradingPipeline.process` стека текущего режима (Risk Gate → `decision_audit` →
   ордер). Других путей к ордеру нет. Бар, по которому уже есть сигналы стратегий, повторно не
   обрабатывается (рестарт не даёт двойного входа). ANALYSIS_ONLY — сигналы не считаются;
   SIGNAL_ONLY — Risk Gate оценивает и отклоняет (R1).
3. Защитные выходы SL/TP — каждый тик для всех стеков (и при CB TRIPPED / KILLED).
4. По расписанию: снимок портфеля + проверка CB по PnL (`snapshot_s`), реконсиляция
   (`execution.reconcile_interval_s`), новости (`news.poll_interval_s`, если есть ключ Finnhub),
   LLM-контекст (`llm_refresh_s`), LLM explain (`explain_s`), переходы LLM-breaker, алерты
   (`alerts_s`).

Сбой задачи не останавливает цикл: `RUN_ERROR` (CRITICAL → алерт) не чаще раза в 15 минут на
задачу. Старт/стоп — `RUN_STARTED` / `RUN_STOPPED`. Ордера при остановке не отменяются: для
аварийной остановки — `augur kill-switch`.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from augur import __version__
from augur.adapters.base import MarketAdapter
from augur.adapters.paper import PaperAdapter
from augur.backtest.costs import CostModel
from augur.config.settings import Settings, TradingMode, config_hash
from augur.data.loader import OhlcvLoader
from augur.data.models import Bar, timeframe_ms
from augur.db.audit import append_audit
from augur.execution.modes import (
    LIVE_MODES,
    ModeController,
    has_live_book,
    live_book_mode,
    mode_rank,
)
from augur.execution.pipeline import LiveStack, PaperStack, build_live_stack, build_paper_stack
from augur.fusion.client import FusionClient
from augur.notify.outbox import AlertDispatcher, build_notifier
from augur.notify.telegram import Notifier
from augur.signals.engine import SignalEngine, StrategyContext, StrategySignal
from augur.signals.repository import SignalRepository
from augur.strategies.base import Strategy
from augur.strategies.registry import get_strategy

__all__ = ["Runner", "live_allowed"]

log = logging.getLogger(__name__)
ACTOR = "orchestrator"
_ERROR_EVERY_MS = 15 * 60_000

Job = Callable[[], Awaitable[object]]


def live_allowed(settings: Settings) -> bool:
    floor = mode_rank(TradingMode.MICRO_LIVE)
    return (
        mode_rank(settings.trading_mode) >= floor and mode_rank(settings.max_allowed_mode) >= floor
    )


@dataclass
class _Sched:
    interval_ms: int
    next_ts: int = 0


@dataclass
class _State:
    last_bar: dict[str, int] = field(default_factory=dict)
    sched: dict[str, _Sched] = field(default_factory=dict)
    last_error: dict[str, int] = field(default_factory=dict)


class Runner:
    """Args:
    conn: соединение (схема текущая).
    settings: настройки.
    clock: «сейчас», Unix ms UTC.
    data_adapter: источник баров и котировок (CCXT; в PAPER — только чтение).
    live_adapter: реальная площадка для live-стека; None — CcxtAdapter из настроек.
    fusion_client: Fusion; None — по настройкам (`build_fusion_client`).
    notifier: канал алертов; None — Telegram из настроек (не настроен — алерты skipped).
    llm_client: LLM-клиент (тесты); None — OpenAI-совместимый httpx.
    news_fetchers: источники новостей (тесты); None — Finnhub, если задан ключ.
    sleep: ожидание между тиками (тесты).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        *,
        clock: Callable[[], int],
        data_adapter: MarketAdapter,
        live_adapter: MarketAdapter | None = None,
        fusion_client: FusionClient | None = None,
        notifier: Notifier | None = None,
        llm_client: Any = None,
        news_fetchers: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        app_version: str = __version__,
    ) -> None:
        self._conn = conn
        self._s = settings
        self._clock = clock
        self._data = data_adapter
        self._live_adapter = live_adapter
        self._llm_client = llm_client
        self._news_fetchers = news_fetchers
        self._sleep = sleep
        self._app = app_version
        self._tf = settings.run.timeframe
        self._tf_ms = timeframe_ms(self._tf)
        self._st = _State()
        self._strategies: list[Strategy] = [
            get_strategy(spec.name, spec.params) for spec in settings.run.strategies
        ]
        if fusion_client is None:
            from augur.fusion.factory import build_fusion_client

            fusion_client = build_fusion_client(conn, settings, clock=clock, llm_client=llm_client)
        self._engine = SignalEngine(
            fusion_client,
            clock=clock,
            fusion_enabled=settings.fusion.enabled,
            fusion_timeout_ms=settings.fusion.timeout_ms,
            repo=SignalRepository(
                conn, config_hash=config_hash(settings), code_version=app_version
            ),
        )
        self._alerts = AlertDispatcher(
            conn,
            settings.notify,
            notifier if notifier is not None else build_notifier(settings),
            clock=clock,
        )
        self.paper: PaperStack | None = None
        self.live: LiveStack | None = None
        self._news: Any = None
        self._stop = asyncio.Event()

    # --- жизненный цикл -------------------------------------------------------------

    async def start(self) -> None:
        await self._data.connect()
        ModeController(self._conn, settings=self._s, clock=self._clock).sync_with_config(
            actor=ACTOR
        )
        self._audit("RUN_STARTED", "INFO", {"timeframe": self._tf, "symbols": self._s.symbols})
        await self._ensure_stacks()

    async def stop(self, *, reason: str) -> None:
        self._stop.set()
        self._audit("RUN_STOPPED", "WARN", {"reason": reason})
        await self._safe("alerts", self._alerts.dispatch)
        if self.live is not None:
            await self.live.venue_adapter.close()
            self.live = None
        await self._data.close()

    def request_stop(self) -> None:
        self._stop.set()

    async def run_forever(self, *, max_ticks: int | None = None) -> None:
        await self.start()
        ticks = 0
        reason = "stopped"
        try:
            while not self._stop.is_set():
                await self.tick()
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                await self._sleep(self._s.run.tick_s)
        except (KeyboardInterrupt, asyncio.CancelledError):
            reason = "interrupted"
            raise
        finally:
            await self.stop(reason=reason)

    # --- тик ------------------------------------------------------------------------

    async def tick(self) -> None:
        await self._safe("stacks", self._ensure_stacks)
        await self._safe("bars", self._on_bars)
        await self._safe("protective_exits", self._protective_exits)
        run, ex, news = self._s.run, self._s.execution, self._s.news
        await self._every("snapshot", run.snapshot_s, self._snapshot)
        await self._every("reconcile", ex.reconcile_interval_s, self._reconcile)
        if run.news and (self._news_fetchers is not None or self._s.finnhub_api_key is not None):
            await self._every("news", news.poll_interval_s, self._poll_news)
        llm = self._s.fusion.llm
        if self._s.fusion.enabled and llm.mode == "enabled":
            await self._every("llm_refresh", run.llm_refresh_s, self._llm_refresh)
        if llm.mode == "enabled" and llm.explain_enabled:
            await self._every("explain", run.explain_s, self._explain)
        await self._safe("llm_breaker", self._llm_breaker)
        await self._every("alerts", run.alerts_s, self._alerts.dispatch)

    async def _every(self, name: str, interval_s: float, job: Job) -> None:
        now = self._clock()
        sch = self._st.sched.setdefault(name, _Sched(int(interval_s * 1000)))
        if now < sch.next_ts:
            return
        sch.next_ts = now + sch.interval_ms
        await self._safe(name, job)

    async def _safe(self, name: str, job: Job) -> None:
        try:
            await job()
        except Exception as e:  # noqa: BLE001 — сбой задачи не останавливает цикл
            log.exception("job %s failed", name)
            now = self._clock()
            if now - self._st.last_error.get(name, -_ERROR_EVERY_MS) >= _ERROR_EVERY_MS:
                self._st.last_error[name] = now
                try:
                    self._conn.rollback()
                    self._audit("RUN_ERROR", "CRITICAL", {"job": name, "error": type(e).__name__})
                except sqlite3.Error:
                    log.exception("cannot write RUN_ERROR")

    def _audit(self, event: str, severity: str, payload: dict[str, object]) -> None:
        try:
            append_audit(
                self._conn,
                actor=ACTOR,
                event=event,
                severity=severity,
                ref_type="run",
                ref_id=self._tf,
                payload=payload,
                ts=self._clock(),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    # --- стеки ----------------------------------------------------------------------

    def _mode(self) -> TradingMode:
        return ModeController(self._conn, settings=self._s, clock=self._clock).current()

    async def _ensure_stacks(self) -> None:
        if self.paper is None:
            ex = self._s.execution
            broker = PaperAdapter(
                start_cash=ex.paper_start_cash,
                costs=CostModel(commission_pct=ex.commission_pct, slippage_pct=ex.slippage_pct),
                price_source=self._data,
            )
            self.paper = build_paper_stack(
                self._conn, self._s, app_version=self._app, clock=self._clock, broker=broker
            )
        if not live_allowed(self._s):
            return
        current = self._mode()
        want = current in LIVE_MODES or any(has_live_book(self._conn, m) for m in LIVE_MODES)
        book = live_book_mode(self._conn, current)
        if self.live is not None and (not want or self.live.orders.mode != book):
            await self.live.venue_adapter.close()
            self.live = None
        if want and self.live is None:
            stack = build_live_stack(
                self._conn,
                self._s,
                app_version=self._app,
                clock=self._clock,
                adapter=self._live_adapter,
                mode=book,
            )
            await stack.venue_adapter.connect()
            self.live = stack

    def _stacks(self) -> list[PaperStack | LiveStack]:
        return [s for s in (self.paper, self.live) if s is not None]

    def _exec_stack(self, mode: TradingMode) -> PaperStack | LiveStack | None:
        if mode in LIVE_MODES:
            return self.live if self.live is not None and self.live.orders.mode == mode else None
        return self.paper

    # --- бары и сигналы -------------------------------------------------------------

    def _last_closed_open(self) -> int:
        now = self._clock() - self._s.run.bar_close_delay_s * 1000
        return now // self._tf_ms * self._tf_ms - self._tf_ms

    def _read_bars(self, symbol: str, end_open: int) -> list[Bar]:
        start = end_open - (self._s.run.history_bars - 1) * self._tf_ms
        rows = self._conn.execute(
            "SELECT venue, ts_open, open, high, low, close, volume FROM ohlcv WHERE venue = ? "
            "AND symbol = ? AND timeframe = ? AND ts_open BETWEEN ? AND ? ORDER BY ts_open",
            (self._data.venue, symbol, self._tf, start, end_open),
        ).fetchall()
        return [
            Bar(
                symbol=symbol,
                venue=str(r[0]),
                timeframe=self._tf,
                ts_open=int(r[1]),
                open=float(r[2]),
                high=float(r[3]),
                low=float(r[4]),
                close=float(r[5]),
                volume=float(r[6]),
                is_closed=True,
            )
            for r in rows
        ]

    def _bar_done(self, symbol: str, bar_open: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM signals WHERE kind = 'strategy' AND symbol = ? AND timeframe = ? "
            "AND bar_ts = ? LIMIT 1",
            (symbol, self._tf, bar_open),
        ).fetchone()
        return row is not None

    async def _on_bars(self) -> None:
        bar_open = self._last_closed_open()
        mode = self._mode()
        for symbol in self._s.symbols:
            if self._st.last_bar.get(symbol, -1) >= bar_open:
                continue
            await self._process_bar(symbol, bar_open, mode)
            self._st.last_bar[symbol] = bar_open

    async def _process_bar(self, symbol: str, bar_open: int, mode: TradingMode) -> None:
        start = bar_open - (self._s.run.history_bars - 1) * self._tf_ms
        await OhlcvLoader(self._conn, self._data).load(
            symbol, self._tf, start, bar_open + self._tf_ms
        )
        if mode == TradingMode.ANALYSIS_ONLY:
            return
        bars = self._read_bars(symbol, bar_open)
        if not bars or bars[-1].ts_open != bar_open or self._bar_done(symbol, bar_open):
            return
        ctx = StrategyContext(
            bar=bars[-1], history=bars[:-1], symbol=symbol, timeframe=self._tf, timestamp=bar_open
        )
        signals: list[StrategySignal] = []
        for strategy in self._strategies:
            if len(bars) >= strategy.warmup_bars:
                signals.extend(strategy.generate_signals(ctx))
        sig = await self._engine.resolve_signal(signals)
        if sig is None:
            return
        stack = self._exec_stack(mode)
        if stack is None:
            log.warning("no execution stack for mode %s: signal %s not routed", mode, sig.signal_id)
            return
        await stack.pipeline.process(sig, bars=bars)

    # --- периодические задачи -------------------------------------------------------

    async def _protective_exits(self) -> None:
        for st in self._stacks():
            await st.orders.check_protective_exits()

    async def _snapshot(self) -> None:
        for st in self._stacks():
            await st.orders.snapshot()
            await st.monitor.on_pnl_update()
            await st.breaker.check_adapter_errors()

    async def _reconcile(self) -> None:
        for st in self._stacks():
            await st.reconciler.run()

    async def _poll_news(self) -> None:
        if self._news is None:
            from augur.news.pipeline import build_news_pipeline

            scorer = None
            if self._s.news.scorer == "llm":
                from augur.fusion.factory import llm_keys
                from augur.fusion.llm.client import OpenAICompatClient
                from augur.fusion.llm.news_scorer import LLMNewsScorer
                from augur.news import RuleBasedScorer

                scorer = LLMNewsScorer(
                    self._conn,
                    self._s.fusion.llm,
                    self._llm_client or OpenAICompatClient(),
                    llm_keys(self._s),
                    fallback=RuleBasedScorer(self._s.news.source_weights),
                    clock=self._clock,
                )
            self._news = build_news_pipeline(
                self._conn, self._s, fetchers=self._news_fetchers, clock=self._clock, scorer=scorer
            )
        await self._news.run_once()

    async def _llm_refresh(self) -> None:
        from augur.fusion.factory import refresh_llm_context

        for symbol in self._s.symbols:
            await refresh_llm_context(
                self._conn,
                self._s,
                symbol=symbol,
                timeframe=self._tf,
                clock=self._clock,
                client=self._llm_client,
            )

    async def _explain(self) -> None:
        from augur.fusion.factory import build_explain_service

        svc = build_explain_service(self._conn, self._s, clock=self._clock, client=self._llm_client)
        await svc.explain_pending()

    async def _llm_breaker(self) -> None:
        from augur.fusion.llm.breaker import sync_breaker_states

        sync_breaker_states(self._conn, self._s.fusion.llm, clock=self._clock)
