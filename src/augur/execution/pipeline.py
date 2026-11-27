"""Сквозной путь PAPER и сборка компонентов (ARCHITECTURE.md §1.2 S4, §7).

`TradingPipeline.process(sig)`: снимок портфеля → `RiskContext` (БД + адаптер) → `RiskGate` →
`decision_audit`
(COMMIT, каждый вердикт) → `OrderManager.handle_verdict` (только approved). Других путей к
`OrderManager.handle_verdict` в коде нет: Risk Gate не обходится.

`build_paper_stack` — минимальный composition root для PAPER (до оркестратора): PaperAdapter
с общей моделью издержек под `ResilientAdapter`, восстановление состояния брокера из SQLite,
CB с хуком ошибок адаптера, kill switch, Reconciler, PostTradeMonitor.
"""

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from augur.adapters.paper import PaperAdapter
from augur.adapters.resilient import CallPolicy, ResilientAdapter
from augur.backtest.costs import CostModel
from augur.config.settings import Settings, TradingMode, config_hash
from augur.data.models import Bar
from augur.execution.circuit_breaker import CircuitBreaker
from augur.execution.kill_switch import KillSwitch
from augur.execution.modes import ModeStore
from augur.execution.order_manager import OrderManager, restore_paper_broker
from augur.execution.reconciler import Reconciler
from augur.execution.repository import OrderRecord
from augur.risk.audit import DecisionAuditRecord, DecisionAuditWriter
from augur.risk.context import RiskContextBuilder, market_snapshot
from augur.risk.gate import RiskGate
from augur.risk.models import RiskVerdict
from augur.risk.post_trade import PostTradeMonitor, RiskMetricsBuilder
from augur.signals.ready import SignalReady, ms_to_dt

__all__ = ["PaperStack", "PipelineResult", "TradingPipeline", "build_paper_stack"]


class PipelineResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    verdict: RiskVerdict
    decision: DecisionAuditRecord
    order: OrderRecord | None


class TradingPipeline:
    def __init__(
        self,
        *,
        gate: RiskGate,
        contexts: RiskContextBuilder,
        audit: DecisionAuditWriter,
        orders: OrderManager,
        breaker: CircuitBreaker,
        clock: Callable[[], int],
    ) -> None:
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
        if verdict.approved:
            order = await self._orders.handle_verdict(verdict, sig, decision_id=decision.id)
        await self._cb.check_adapter_errors()
        return PipelineResult(verdict=verdict, decision=decision, order=order)


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
    """Собрать PAPER-исполнение. `broker` — для тестов (своя синтетика/часы).

    Raises:
        ExecutionRefusedError: `settings.trading_mode` не PAPER.
    """
    ex = settings.execution
    broker = broker or PaperAdapter(
        start_cash=ex.paper_start_cash,
        costs=CostModel(commission_pct=ex.commission_pct, slippage_pct=ex.slippage_pct),
    )
    if restore:
        restore_paper_broker(broker, conn, mode=TradingMode.PAPER, start_cash=ex.paper_start_cash)
    cfg_hash = config_hash(settings)
    adapter = ResilientAdapter(broker, policy)
    modes = ModeStore(conn, configured_mode=settings.trading_mode)
    orders = OrderManager(
        conn,
        adapter,
        mode=settings.trading_mode,
        app_version=app_version,
        config_hash=cfg_hash,
        settings=ex,
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
        RiskMetricsBuilder(conn, mode=TradingMode.PAPER.value, fusion=settings.fusion),
        breaker,
        snapshot=orders.snapshot,
    )
    orders.on_fill = monitor.on_fill
    pipeline = TradingPipeline(
        gate=RiskGate(settings.risk),
        contexts=RiskContextBuilder(conn, configured_mode=settings.trading_mode),
        audit=DecisionAuditWriter(conn, app_version=app_version, config_hash=cfg_hash),
        orders=orders,
        breaker=breaker,
        clock=clock,
    )
    return PaperStack(
        broker=broker,
        adapter=adapter,
        modes=modes,
        orders=orders,
        breaker=breaker,
        kill_switch=KillSwitch(conn, orders=orders, modes=modes, clock=clock),
        reconciler=Reconciler(conn, orders=orders, breaker=breaker, clock=clock),
        monitor=monitor,
        pipeline=pipeline,
    )
