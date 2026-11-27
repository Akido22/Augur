"""Сквозной путь PAPER / MICRO_LIVE и сборка компонентов (ARCHITECTURE.md §1.2 S4, §7).

`TradingPipeline.process(sig)`: снимок портфеля → `RiskContext` (БД + адаптер) → `RiskGate` →
`decision_audit`
(COMMIT, каждый вердикт) → `OrderManager.handle_verdict` (только approved). Других путей к
`OrderManager.handle_verdict` в коде нет: Risk Gate не обходится.

`build_paper_stack` — минимальный composition root для PAPER (до оркестратора): PaperAdapter
с общей моделью издержек под `ResilientAdapter`, восстановление состояния брокера из SQLite,
CB с хуком ошибок адаптера, kill switch, Reconciler, PostTradeMonitor.

`build_live_stack` (0.7.0) — то же для MICRO_LIVE над реальным адаптером (CCXT spot): без
восстановления брокера (истина — площадка, сверка — Reconciler), с подтверждением первого
ордера (`FirstOrderApproval`) и сверкой только `settings.symbols`. Стек собирается, если
конфиг разрешает MICRO_LIVE; ТЕКУЩИЙ режим он не повышает. Пока режим ниже MICRO_LIVE (до
`augur mode set`, после CB/kill switch), одобренные вердикты не исполняются:
`EXECUTION_SKIPPED`. Kill switch, SL/TP и Reconciler работают при любом режиме.
"""

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from augur.adapters.base import MarketAdapter
from augur.adapters.ccxt_adapter import CcxtAdapter  # SDK ccxt импортируется лениво в connect()
from augur.adapters.paper import PaperAdapter
from augur.adapters.resilient import CallPolicy, ResilientAdapter
from augur.backtest.costs import CostModel
from augur.config.settings import Settings, TradingMode, config_hash
from augur.data.models import Bar
from augur.db.audit import append_audit
from augur.execution.approval import FirstOrderApproval
from augur.execution.circuit_breaker import CircuitBreaker
from augur.execution.kill_switch import KillSwitch
from augur.execution.modes import ModeStore, mode_rank
from augur.execution.order_manager import (
    ExecutionRefusedError,
    OrderManager,
    restore_paper_broker,
)
from augur.execution.reconciler import Reconciler
from augur.execution.repository import OrderRecord
from augur.risk.audit import DecisionAuditRecord, DecisionAuditWriter
from augur.risk.context import RiskContextBuilder, market_snapshot
from augur.risk.gate import RiskGate
from augur.risk.models import RiskVerdict
from augur.risk.post_trade import PostTradeMonitor, RiskMetricsBuilder
from augur.signals.ready import SignalReady, ms_to_dt

__all__ = [
    "LiveStack",
    "PaperStack",
    "PipelineResult",
    "TradingPipeline",
    "build_live_stack",
    "build_paper_stack",
]

log = logging.getLogger(__name__)


class PipelineResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    verdict: RiskVerdict
    decision: DecisionAuditRecord
    order: OrderRecord | None


class TradingPipeline:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        gate: RiskGate,
        contexts: RiskContextBuilder,
        audit: DecisionAuditWriter,
        orders: OrderManager,
        breaker: CircuitBreaker,
        clock: Callable[[], int],
    ) -> None:
        self._conn = conn
        self._gate = gate
        self._contexts = contexts
        self._audit = audit
        self._orders = orders
        self._cb = breaker
        self._clock = clock

    async def process(self, sig: SignalReady, *, bars: Sequence[Bar] = ()) -> PipelineResult:
        """Сигнал (уже записан в `signals` как final) → вердикт → аудит → ордер."""
        now: datetime = ms_to_dt(self._clock())
        await self._orders.snapshot()  # mark-to-market: R10 day_pnl по текущему счёту
        market = await market_snapshot(self._orders.adapter, sig.symbol, bars=bars)
        ctx = self._contexts.build(sig, market, now=now)
        verdict = await self._gate.evaluate(sig, ctx)
        decision = self._audit.record(sig, verdict)  # COMMIT до отправки (VERSIONING §6.2)
        order = None
        if verdict.approved and verdict.mode != self._orders.mode:
            self._skip(sig, verdict, decision.id)
        elif verdict.approved:
            order = await self._orders.handle_verdict(verdict, sig, decision_id=decision.id)
        await self._cb.check_adapter_errors()
        return PipelineResult(verdict=verdict, decision=decision, order=order)

    def _skip(self, sig: SignalReady, verdict: RiskVerdict, decision_id: str) -> None:
        """Режим (mode_state) не совпадает с режимом стека — ордер не создаётся (COMMIT)."""
        log.warning("execution skipped: mode %s, stack %s", verdict.mode, self._orders.mode)
        try:
            append_audit(
                self._conn,
                actor="execution",
                event="EXECUTION_SKIPPED",
                severity="WARN",
                ref_type="signal",
                ref_id=sig.signal_id,
                payload={
                    "mode": verdict.mode.value,
                    "stack_mode": self._orders.mode.value,
                    "decision_audit_id": decision_id,
                },
                ts=self._clock(),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise


@dataclass
class PaperStack:
    broker: PaperAdapter
    adapter: ResilientAdapter
    modes: ModeStore
    orders: OrderManager
    breaker: CircuitBreaker
    kill_switch: KillSwitch
    reconciler: Reconciler
    monitor: PostTradeMonitor
    pipeline: TradingPipeline


@dataclass
class LiveStack:
    venue_adapter: MarketAdapter  # сырой адаптер площадки: connect()/close() — на вызывающем
    adapter: ResilientAdapter
    modes: ModeStore
    orders: OrderManager
    breaker: CircuitBreaker
    kill_switch: KillSwitch
    reconciler: Reconciler
    monitor: PostTradeMonitor
    pipeline: TradingPipeline
    approval: FirstOrderApproval


@dataclass
class _Parts:
    adapter: ResilientAdapter
    modes: ModeStore
    orders: OrderManager
    breaker: CircuitBreaker
    kill_switch: KillSwitch
    reconciler: Reconciler
    monitor: PostTradeMonitor
    pipeline: TradingPipeline


def _assemble(
    conn: sqlite3.Connection,
    settings: Settings,
    inner: MarketAdapter,
    *,
    mode: TradingMode,
    app_version: str,
    clock: Callable[[], int],
    policy: CallPolicy | None,
    reconcile_symbols: Sequence[str] | None,
) -> _Parts:
    cfg_hash = config_hash(settings)
    adapter = ResilientAdapter(inner, policy)
    modes = ModeStore(conn, configured_mode=settings.trading_mode)
    orders = OrderManager(
        conn,
        adapter,
        mode=mode,
        app_version=app_version,
        config_hash=cfg_hash,
        settings=settings.execution,
        clock=clock,
    )
    breaker = CircuitBreaker(
        conn,
        orders=orders,
        modes=modes,
        limits=settings.risk,
        cb=settings.cb,
        fusion=settings.fusion,
        clock=clock,
    )
    adapter.on_error = breaker.record_adapter_error
    monitor = PostTradeMonitor(
        RiskMetricsBuilder(conn, mode=mode.value, fusion=settings.fusion),
        breaker,
        snapshot=orders.snapshot,
    )
    orders.on_fill = monitor.on_fill
    pipeline = TradingPipeline(
        conn,
        gate=RiskGate(settings.risk),
        contexts=RiskContextBuilder(conn, configured_mode=settings.trading_mode),
        audit=DecisionAuditWriter(conn, app_version=app_version, config_hash=cfg_hash),
        orders=orders,
        breaker=breaker,
        clock=clock,
    )
    return _Parts(
        adapter=adapter,
        modes=modes,
        orders=orders,
        breaker=breaker,
        kill_switch=KillSwitch(conn, orders=orders, modes=modes, clock=clock),
        reconciler=Reconciler(
            conn, orders=orders, breaker=breaker, clock=clock, symbols=reconcile_symbols
        ),
        monitor=monitor,
        pipeline=pipeline,
    )


def build_paper_stack(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    app_version: str,
    clock: Callable[[], int],
    broker: PaperAdapter | None = None,
    policy: CallPolicy | None = None,
    restore: bool = True,
) -> PaperStack:
    """Собрать PAPER-исполнение. `broker` — для тестов (своя синтетика/часы)."""
    ex = settings.execution
    broker = broker or PaperAdapter(
        start_cash=ex.paper_start_cash,
        costs=CostModel(commission_pct=ex.commission_pct, slippage_pct=ex.slippage_pct),
    )
    if restore:
        restore_paper_broker(broker, conn, mode=TradingMode.PAPER, start_cash=ex.paper_start_cash)
    parts = _assemble(
        conn,
        settings,
        broker,
        mode=TradingMode.PAPER,
        app_version=app_version,
        clock=clock,
        policy=policy,
        reconcile_symbols=None,
    )
    return PaperStack(broker=broker, **vars(parts))


def build_live_stack(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    app_version: str,
    clock: Callable[[], int],
    adapter: MarketAdapter | None = None,
    policy: CallPolicy | None = None,
) -> LiveStack:
    """Собрать MICRO_LIVE-исполнение над реальной площадкой (по умолчанию — CCXT spot).

    Адаптер не подключается: `await stack.venue_adapter.connect()` — на вызывающем.

    Raises:
        ExecutionRefusedError: конфиг не разрешает MICRO_LIVE, нет ключей CCXT или адаптер `paper`.
    """
    for name, value in (
        ("trading_mode", settings.trading_mode),
        ("max_allowed_mode", settings.max_allowed_mode),
    ):
        if mode_rank(value) < mode_rank(TradingMode.MICRO_LIVE):
            raise ExecutionRefusedError(f"settings.{name}={value} does not allow MICRO_LIVE")
    if adapter is None:
        if settings.ccxt_api_key is None or settings.ccxt_api_secret is None:
            raise ExecutionRefusedError("MICRO_LIVE requires CCXT_API_KEY and CCXT_API_SECRET")
        adapter = CcxtAdapter(settings)
    parts = _assemble(
        conn,
        settings,
        adapter,
        mode=TradingMode.MICRO_LIVE,
        app_version=app_version,
        clock=clock,
        policy=policy,
        reconcile_symbols=settings.symbols,
    )
    approval = FirstOrderApproval(
        conn,
        orders=parts.orders,
        modes=parts.modes,
        clock=clock,
        code_ttl_s=settings.mode.code_ttl_s,
    )
    return LiveStack(venue_adapter=adapter, approval=approval, **vars(parts))
