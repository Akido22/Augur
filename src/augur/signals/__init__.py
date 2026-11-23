"""Signal Engine (ARCHITECTURE.md §2.7). MUST NOT содержать ML-кода и вызывать LLM."""

from augur.signals.engine import SignalEngine, StrategyContext, StrategySignal

__all__ = ["SignalEngine", "StrategyContext", "StrategySignal"]
