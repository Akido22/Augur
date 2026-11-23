"""CLI: augur strategies list, augur backtest (данные — PaperAdapter, без сети)."""

import pytest
import typer
from typer.testing import CliRunner

from augur.interfaces.cli.main import app, parse_params

runner = CliRunner()


def test_strategies_list() -> None:
    res = runner.invoke(app, ["strategies", "list"])
    assert res.exit_code == 0
    assert "sma_cross" in res.output and "short=10,long=30" in res.output
    assert "rsi" in res.output and "period=14" in res.output


def test_parse_params() -> None:
    assert parse_params("short=10, long=30") == {"short": "10", "long": "30"}
    assert parse_params("") == {}
    with pytest.raises(typer.BadParameter):
        parse_params("short")


def test_backtest_prints_metrics() -> None:
    args = [
        "backtest",
        "--strategy",
        "sma_cross",
        "--params",
        "short=10,long=30",
        "--asset",
        "BTC/USDT",
        "--timeframe",
        "1h",
        "--since",
        "2024-01-01",
        "--until",
        "2024-01-15",
        "--data-source",
        "paper",
    ]
    res = runner.invoke(app, args)
    assert res.exit_code == 0, res.output
    assert "(360 bars)" in res.output  # 15 дней × 24, --until включительно
    for metric in ("total_return", "sharpe", "sortino", "max_drawdown", "profit_factor"):
        assert metric in res.output
    assert runner.invoke(app, args).output == res.output  # детерминизм


@pytest.mark.parametrize(
    "extra",
    [
        ["--strategy", "nope"],
        ["--strategy", "sma_cross", "--params", "short=40"],
        ["--strategy", "rsi", "--data-source", "ccxt"],
    ],
)
def test_backtest_errors(extra: list[str]) -> None:
    res = runner.invoke(app, ["backtest", *extra, "--until", "2024-01-03"])
    assert res.exit_code == 2
