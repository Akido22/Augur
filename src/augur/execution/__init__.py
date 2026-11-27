"""Execution / Auto-Trading (ARCHITECTURE.md §2.9, §7) — PAPER (0.6.0) и MICRO_LIVE (0.7.0).

Путь к площадке только один: `RiskGate` → `decision_audit` (COMMIT) → `OrderManager`.
Публичные классы: `ModeController`, `OrderManager`, `PositionManager`, `CircuitBreaker`,
`KillSwitch`, `Reconciler`, `FirstOrderApproval`, `ModeStore`.
"""

from augur.execution.approval import (
    ApprovalError,
    ApprovalTicket,
    FirstOrderApproval,
    InvalidApprovalCodeError,
)
from augur.execution.circuit_breaker import CBStatus, CircuitBreaker, InvalidResetCodeError
from augur.execution.idempotency import make_client_order_id
from augur.execution.kill_switch import KillReport, KillSwitch
from augur.execution.modes import (
    InvalidModeCodeError,
    ModeChangeError,
    ModeChangeTicket,
    ModeController,
    ModeStore,
    PromotionStatus,
)
from augur.execution.order_manager import ExecutionRefusedError, OrderManager, restore_paper_broker
from augur.execution.pipeline import (
    LiveStack,
    PaperStack,
    PipelineResult,
    TradingPipeline,
    build_live_stack,
    build_paper_stack,
)
from augur.execution.positions import PortfolioSnapshot, PortfolioTracker, PositionManager
from augur.execution.reconciler import Reconciler, ReconcileReport
from augur.execution.repository import OrderRecord, OrderRepository
from augur.execution.state_machine import ALLOWED_TRANSITIONS, IllegalTransitionError

__all__ = [
    "ALLOWED_TRANSITIONS",
    "ApprovalError",
    "ApprovalTicket",
    "FirstOrderApproval",
    "InvalidApprovalCodeError",
    "InvalidModeCodeError",
    "LiveStack",
    "ModeChangeError",
    "ModeChangeTicket",
    "ModeController",
    "PromotionStatus",
    "build_live_stack",
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
