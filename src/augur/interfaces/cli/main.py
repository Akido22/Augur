"""Точка входа CLI `augur` (pyproject: `augur.interfaces.cli.main:app`)."""

import asyncio
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from augur import __version__
from augur.adapters.paper import PaperAdapter
from augur.backtest import Backtester, BacktestResult
from augur.config.settings import Settings
from augur.data.models import Bar
from augur.db.migrator import MigrationError, Migrator
from augur.strategies import (
    StrategyParamsError,
    UnknownStrategyError,
    get_strategy,
    list_strategies,
)

app = typer.Typer(name="augur", help="Augur — гибридная торговая система ML+LLM.")
db_app = typer.Typer(help="Схема SQLite: миграции и бэкапы.", no_args_is_help=True)
app.add_typer(db_app, name="db")
strategies_app = typer.Typer(help="Стратегии: реестр и параметры.", no_args_is_help=True)
app.add_typer(strategies_app, name="strategies")

# Значение по умолчанию берём из модели Settings без загрузки .env:
# командам БД не нужны секреты (api_auth_token и пр.).
DEFAULT_DB_PATH: str = Settings.model_fields["db_path"].default
DbOption = typer.Option(DEFAULT_DB_PATH, "--db", help="Путь к файлу SQLite.")


def _version_callback(value: bool) -> None:
    """Печатает версию приложения и завершает работу."""
    if value:
        typer.echo(f"augur {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Показать версию и выйти.",
    ),
) -> None:
    """Augur CLI. Без команды печатает справку."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


def _fmt_ts(ms: int | None) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


@db_app.command("migrate")
def db_migrate(db: str = DbOption) -> None:
    """Применить неприменённые миграции (с бэкапом перед первой)."""
    try:
        result = Migrator().migrate(db)
    except MigrationError as e:
        typer.echo(f"ERROR: {e}", err=True)
        raise typer.Exit(code=1) from e
    if not result.applied:
        typer.echo(f"up to date: schema {result.schema_version} ({db})")
        return
    typer.echo(f"backup: {result.backup_path}")
    for name in result.applied:
        typer.echo(f"applied: {name}")
    typer.echo(f"schema {result.schema_version} ({db})")


@db_app.command("status")
def db_status(db: str = DbOption) -> None:
    """Таблица version / name / state / applied_at / checksum."""
    try:
        rows = Migrator().status(db)
    except MigrationError as e:
        typer.echo(f"ERROR: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"{'version':>7}  {'name':<24} {'state':<12} {'applied_at':<23} checksum")
    for r in rows:
        typer.echo(
            f"{r.version:>7}  {r.name:<24} {r.state:<12} {_fmt_ts(r.applied_at):<23} "
            f"{r.checksum[:12]}"
        )


@db_app.command("backup")
def db_backup(db: str = DbOption) -> None:
    """Снять бэкап БД в backups/ (VACUUM INTO)."""
    if not Path(db).is_file():
        typer.echo(f"ERROR: database not found: {db}", err=True)
        raise typer.Exit(code=1)
    try:
        path = Migrator().backup_before_migrate(db)
    except Exception as e:  # noqa: BLE001 — показываем пользователю любую ошибку SQLite/ФС
        typer.echo(f"ERROR: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"backup: {path}")


# --- стратегии и бэктест (0.3.0) ------------------------------------------------


@strategies_app.command("list")
def strategies_list() -> None:
    """Таблица name / version / params / warmup_bars."""
    rows = list_strategies()
    typer.echo(f"{'name':<12} {'version':<14} {'warmup_bars':>11}  params")
    for r in rows:
        params = ",".join(f"{k}={v}" for k, v in r["params"].items())
        typer.echo(f"{r['name']:<12} {r['version']:<14} {r['warmup_bars']:>11}  {params}")


def parse_params(raw: str) -> dict[str, Any]:
    """`short=10,long=30` → {"short": "10", "long": "30"}; типы приводит `params_model`.

    Raises:
        typer.BadParameter: элемент без `=` или пустой ключ.
    """
    out: dict[str, Any] = {}
    for item in filter(None, (x.strip() for x in raw.split(","))):
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise typer.BadParameter(f"expected key=value, got {item!r}", param_hint="--params")
        out[key.strip()] = value.strip()
    return out


def _parse_date(value: str, option: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise typer.BadParameter(f"expected YYYY-MM-DD, got {value!r}", param_hint=option) from e


def _to_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


async def _load_paper_bars(
    symbol: str, timeframe: str, start: int, end: int, seed: int
) -> list[Bar]:
    adapter = PaperAdapter(seed=seed, clock_ms=end)
    await adapter.connect()
    try:
        return await adapter.get_historical(symbol, timeframe, start, end)
    finally:
        await adapter.close()


def _fmt_metric(name: str, value: float) -> str:
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    if name in ("total_return", "annualized_return", "max_drawdown", "win_rate"):
        return f"{value * 100:.2f}%"
    if name == "num_trades":
        return str(int(value))
    return f"{value:.4f}"


def _print_result(result: BacktestResult, initial_cash: float) -> None:
    final = result.equity_curve[-1][1] if result.equity_curve else initial_cash
    typer.echo(f"initial cash:      {initial_cash:.2f}")
    typer.echo(f"final equity:      {final:.2f}")
    for name, value in result.metrics.items():
        typer.echo(f"{name + ':':<18} {_fmt_metric(name, value)}")


@app.command("backtest")
def backtest(
    strategy: str = typer.Option(..., "--strategy", help="Имя стратегии (augur strategies list)."),
    params: str = typer.Option("", "--params", help="Параметры: key=value,key=value."),
    asset: str = typer.Option("BTC/USDT", "--asset", "--symbol", help="Инструмент."),
    timeframe: str = typer.Option("1h", "--timeframe", "--tf", help="Таймфрейм."),
    since: str = typer.Option("2024-01-01", "--since", "--from", help="Начало, YYYY-MM-DD (UTC)."),
    until: str = typer.Option(
        "2024-12-31", "--until", "--to", help="Конец, YYYY-MM-DD (UTC), день включительно."
    ),
    data_source: str = typer.Option("paper", "--data-source", help="Источник данных: paper."),
    cash: float = typer.Option(10_000.0, "--cash", help="Стартовый капитал."),
    commission: float = typer.Option(0.1, "--commission", help="Комиссия, % от notional."),
    slippage: float = typer.Option(0.05, "--slippage", help="Проскальзывание, % от цены."),
    fill_on: str = typer.Option("close", "--fill-on", help="Исполнение: close | next_open."),
    seed: int = typer.Option(42, "--seed", help="Seed синтетических данных (paper)."),
) -> None:
    """Бэктест стратегии на истории; печатает метрики. Ордера не отправляются."""
    if data_source != "paper":
        typer.echo(f"ERROR: unsupported --data-source {data_source!r}; available: paper", err=True)
        raise typer.Exit(code=2)
    if fill_on not in ("close", "next_open"):
        raise typer.BadParameter("expected close or next_open", param_hint="--fill-on")
    start_d, until_d = _parse_date(since, "--since"), _parse_date(until, "--until")
    if until_d < start_d:
        raise typer.BadParameter("--until must be >= --since", param_hint="--until")
    start_ms, end_ms = _to_ms(start_d), _to_ms(until_d + timedelta(days=1))

    try:
        strat = get_strategy(strategy, parse_params(params))
    except (UnknownStrategyError, StrategyParamsError) as e:
        typer.echo(f"ERROR: {e}", err=True)
        raise typer.Exit(code=2) from e

    try:
        bars = asyncio.run(_load_paper_bars(asset, timeframe, start_ms, end_ms, seed))
    except ValueError as e:
        typer.echo(f"ERROR: {e}", err=True)
        raise typer.Exit(code=2) from e
    if not bars:
        typer.echo("ERROR: no data for the requested range", err=True)
        raise typer.Exit(code=1)

    result = Backtester().run(
        strat,
        bars,
        initial_cash=cash,
        commission_pct=commission,
        slippage_pct=slippage,
        fill_on="close" if fill_on == "close" else "next_open",
    )
    params_s = ",".join(f"{k}={v}" for k, v in strat.params.items())
    typer.echo(f"strategy:          {strat.name} ({strat.version}) {params_s}")
    typer.echo(
        f"data:              {data_source} {asset} {timeframe} {start_d}..{until_d} "
        f"({len(bars)} bars)"
    )
    typer.echo(
        f"costs:             commission {commission}% | slippage {slippage}% | fill {fill_on}"
    )
    _print_result(result, cash)
