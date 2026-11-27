"""Execution / Auto-Trading (ARCHITECTURE.md §2.9, §7) — 0.6.0: режим PAPER.

Путь к площадке только один: `RiskGate` → `decision_audit` (COMMIT) → `OrderManager`.
Публичные классы: `OrderManager`, `PositionManager`, `CircuitBreaker`, `KillSwitch`,
`Reconciler`, `ModeStore` (минимальный; `ModeController` с кодами — 0.7.0).
"""

from augur.execution.circuit_breaker import CBStatus, CircuitBreaker, InvalidResetCodeError
from augur.execution.idempotency import make_client_order_id
from augur.execution.kill_switch import KillReport, KillSwitch
from augur.execution.modes import ModeStore
from augur.execution.order_manager import ExecutionRefusedError, OrderManager, restore_paper_broker
from augur.execution.pipeline import PaperStack, PipelineResult, TradingPipeline, build_paper_stack
from augur.execution.positions import PortfolioSnapshot, PortfolioTracker, PositionManager
from augur.execution.reconciler import Reconciler, ReconcileReport
from augur.execution.repository import OrderRecord, OrderRepository
from augur.execution.state_machine import ALLOWED_TRANSITIONS, IllegalTransitionError

__all__ = [
    "ALLOWED_TRANSITIONS",
    "CBStatus",
    "CircuitBreaker",
    "ExecutionRefusedError",
    "IllegalTransitionError",
    "InvalidResetCodeError",
    "KillReport",
    "KillSwitch",
    "ModeStore",
    "OrderManager",
    "OrderRecord",
    "OrderRepository",
    "PaperStack",
    "PipelineResult",
    "PortfolioSnapshot",
    "PortfolioTracker",
    "PositionManager",
    "ReconcileReport",
    "Reconciler",
    "TradingPipeline",
    "build_paper_stack",
    "make_client_order_id",
    "restore_paper_broker",
]
