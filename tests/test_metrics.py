"""Метрики на эталонных рядах (значения посчитаны вручную)."""

import math

import pytest

from augur.backtest import metrics as m

# Доходности: +10 %, −10 %, +10 %
EQUITY = [100.0, 110.0, 99.0, 108.9]
RETURNS = [0.1, -0.1, 0.1]
PNLS = [10.0, -5.0, 20.0, -15.0, 0.0]


def test_period_returns() -> None:
    assert m.period_returns(EQUITY) == pytest.approx(RETURNS)


def test_total_return() -> None:
    assert m.total_return(EQUITY) == pytest.approx(0.089)
    assert m.total_return([100.0]) == 0.0


def test_annualized_return() -> None:
    # 3 периода; при 3 периодах в году CAGR = total_return
    assert m.annualized_return(EQUITY, 3) == pytest.approx(0.089)
    assert m.annualized_return(EQUITY, 12) == pytest.approx(1.089**4 - 1)
    assert m.annualized_return([100.0, 0.0], 12) == -1.0


def test_sharpe() -> None:
    # mean = 1/30, выборочное std = sqrt(0.02/1.5) = 0.11547
    assert m.sharpe(RETURNS, 1) == pytest.approx((1 / 30) / math.sqrt(0.04 / 3))
    assert m.sharpe(RETURNS, 4) == pytest.approx(2 * m.sharpe(RETURNS, 1))
    assert m.sharpe([0.01, 0.01], 1) == math.inf
    assert m.sharpe([0.0, 0.0], 1) == 0.0


def test_sortino() -> None:
    # downside = sqrt(0.01 / 3)
    assert m.sortino(RETURNS, 1) == pytest.approx((1 / 30) / math.sqrt(0.01 / 3))
    assert m.sortino([0.01, 0.02], 1) == math.inf
    assert m.sortino([], 1) == 0.0


def test_max_drawdown() -> None:
    assert m.max_drawdown(EQUITY) == pytest.approx(0.1)
    assert m.max_drawdown([100, 120, 90, 95, 130, 104]) == pytest.approx(0.25)
    assert m.max_drawdown([1, 2, 3]) == 0.0


def test_trade_metrics() -> None:
    assert m.win_rate(PNLS) == pytest.approx(0.4)
    assert m.profit_factor(PNLS) == pytest.approx(30 / 20)
    assert m.avg_trade(PNLS) == pytest.approx(2.0)
    assert m.win_rate([]) == 0.0
    assert m.profit_factor([5.0]) == math.inf
    assert m.profit_factor([]) == 0.0


def test_compute_metrics_keys_and_values() -> None:
    res = m.compute_metrics(EQUITY, PNLS, 3)
    assert set(res) == {
        "total_return",
        "annualized_return",
        "sharpe",
        "sortino",
        "max_drawdown",
        "win_rate",
        "profit_factor",
        "num_trades",
        "avg_trade",
    }
    assert res["num_trades"] == 5.0
    assert res["max_drawdown"] == pytest.approx(0.1)
