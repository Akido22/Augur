"""Signal Engine (ARCHITECTURE.md §2.7). MUST NOT содержать ML-кода и вызывать LLM."""

from augur.signals.engine import SignalEngine, StrategyContext, StrategySignal
from augur.signals.ready import SignalReady, combine_classic
from augur.signals.repository import SignalRepository

__all__ = [
    "SignalEngine",
    "SignalReady",
    "SignalRepository",
    "StrategyContext",
    "StrategySignal",
    "combine_classic",
]
