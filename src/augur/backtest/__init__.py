"""Backtest Engine: bar-by-bar прогон без look-ahead, издержки, метрики (ARCHITECTURE.md §6.3)."""

from augur.backtest.engine import Backtester, BacktestResult, FillOn, Trade
from augur.backtest.metrics import compute_metrics

__all__ = ["BacktestResult", "Backtester", "FillOn", "Trade", "compute_metrics"]
