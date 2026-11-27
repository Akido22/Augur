"""Risk Gate (ARCHITECTURE.md §2.8, §7.2): pre-trade checks R1–R18, sizing, `decision_audit`.

Gate может только уменьшить объём или отклонить сигнал. Любой путь к Execution идёт через него.
"""

from augur.risk.audit import (
    DecisionAuditRecord,
    DecisionAuditWriter,
    UnauditableOrderError,
    assert_auditable,
)
from augur.risk.context import MarketSnapshot, RiskContextBuilder, market_snapshot, return_zscore
from augur.risk.gate import CHECK_NAMES, RiskGate
from augur.risk.models import CheckResult, RiskContext, RiskVerdict
from augur.risk.sizing import FixedFractionalSizer, PositionSizer

__all__ = [
    "CHECK_NAMES",
    "CheckResult",
    "DecisionAuditRecord",
    "DecisionAuditWriter",
    "FixedFractionalSizer",
    "MarketSnapshot",
    "PositionSizer",
    "RiskContext",
    "RiskContextBuilder",
    "RiskGate",
    "RiskVerdict",
    "UnauditableOrderError",
    "assert_auditable",
    "market_snapshot",
    "return_zscore",
]
