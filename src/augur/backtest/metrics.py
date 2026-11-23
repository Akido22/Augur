"""Метрики бэктеста (ARCHITECTURE.md §6.3). Только stdlib `math`, без numpy/pandas.

Соглашения:
- доходности — доли (0.05 = 5 %), просадка — положительная доля (0.25 = −25 % от пика);
- Sharpe/Sortino аннуализируются через `sqrt(periods_per_year)`, rf = 0;
- Sharpe — по выборочному стандартному отклонению (n − 1);
- Sortino — downside deviation относительно 0 по всем периодам: sqrt(Σ min(r, 0)² / n);
- если знаменатель равен 0: при положительном числителе → `inf`, иначе → 0.0.
"""

import math
from collections.abc import Sequence


def period_returns(equity: Sequence[float]) -> list[float]:
    """Доходности между соседними точками equity: e[t] / e[t-1] − 1."""
    out: list[float] = []
    for prev, curr in zip(equity[:-1], equity[1:], strict=True):
        out.append(curr / prev - 1.0 if prev != 0 else 0.0)
    return out


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def total_return(equity: Sequence[float]) -> float:
    if len(equity) < 2 or equity[0] == 0:
        return 0.0
    return equity[-1] / equity[0] - 1.0


def annualized_return(equity: Sequence[float], periods_per_year: float) -> float:
    """CAGR: (1 + total_return) ^ (periods_per_year / число периодов) − 1."""
    periods = len(equity) - 1
    if periods < 1 or equity[0] <= 0:
        return 0.0
    growth = equity[-1] / equity[0]
    if growth <= 0:
        return -1.0
    return float(math.pow(growth, periods_per_year / periods)) - 1.0


def sharpe(returns: Sequence[float], periods_per_year: float) -> float:
    n = len(returns)
    if n < 2:
        return 0.0
    mean = _mean(returns)
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return math.inf if mean > 0 else 0.0
    return mean / std * math.sqrt(periods_per_year)


def sortino(returns: Sequence[float], periods_per_year: float) -> float:
    n = len(returns)
    if n == 0:
        return 0.0
    mean = _mean(returns)
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / n)
    if downside == 0:
        return math.inf if mean > 0 else 0.0
    return mean / downside * math.sqrt(periods_per_year)


def max_drawdown(equity: Sequence[float]) -> float:
    """Максимальная просадка от пика как положительная доля."""
    peak = -math.inf
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def win_rate(trade_pnls: Sequence[float]) -> float:
    if not trade_pnls:
        return 0.0
    return sum(1 for p in trade_pnls if p > 0) / len(trade_pnls)


def profit_factor(trade_pnls: Sequence[float]) -> float:
    gross_profit = sum(p for p in trade_pnls if p > 0)
    gross_loss = -sum(p for p in trade_pnls if p < 0)
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def avg_trade(trade_pnls: Sequence[float]) -> float:
    return _mean(trade_pnls)


def compute_metrics(
    equity: Sequence[float], trade_pnls: Sequence[float], periods_per_year: float
) -> dict[str, float]:
    """Все метрики разом. `trade_pnls` — чистый PnL закрытых сделок в валюте счёта."""
    returns = period_returns(equity)
    return {
        "total_return": total_return(equity),
        "annualized_return": annualized_return(equity, periods_per_year),
        "sharpe": sharpe(returns, periods_per_year),
        "sortino": sortino(returns, periods_per_year),
        "max_drawdown": max_drawdown(equity),
        "win_rate": win_rate(trade_pnls),
        "profit_factor": profit_factor(trade_pnls),
        "num_trades": float(len(trade_pnls)),
        "avg_trade": avg_trade(trade_pnls),
    }
