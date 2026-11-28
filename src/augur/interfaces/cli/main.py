"""Точка входа CLI `augur` (pyproject: `augur.interfaces.cli.main:app`)."""

import asyncio
import getpass
import json
import math
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import typer
from pydantic import ValidationError

from augur import __version__
from augur.adapters.base import MarketAdapter
from augur.adapters.paper import PaperAdapter
from augur.backtest import Backtester, BacktestResult
from augur.config.settings import Settings, TradingMode, load_settings
from augur.data.models import Bar
from augur.db.migrator import MigrationError, Migrator
from augur.execution import (
    ApprovalError,
    CircuitBreaker,
    ExecutionRefusedError,
    InvalidResetCodeError,
    LiveStack,
    ModeChangeError,
    ModeController,
    ModeStore,
    OrderRepository,
    build_live_stack,
)
from augur.execution.approval import awaiting_approval, first_order_confirmed
from augur.execution.modes import mode_rank
from augur.interfaces.control import engage_kill_switch, live_allowed
from augur.interfaces.dto import DecisionChainDTO
from augur.interfaces.queries import ChainNotFoundError, ReadService, verify_audit_chain
from augur.news import (
    NewsFetcher,
    NewsReactionRules,
    NewsRepository,
    ReactionNotFoundError,
    build_news_pipeline,
)
from augur.strategies import (
    StrategyParamsError,
    UnknownStrategyError,
    get_strategy,
    list_strategies,
)
from augur.versions import API_VERSION, DB_SCHEMA_VERSION

app = typer.Typer(name="augur", help="Augur — гибридная торговая система ML+LLM.")
db_app = typer.Typer(help="Схема SQLite: миграции и бэкапы.", no_args_is_help=True)
app.add_typer(db_app, name="db")
strategies_app = typer.Typer(help="Стратегии: реестр и параметры.", no_args_is_help=True)
app.add_typer(strategies_app, name="strategies")
cb_app = typer.Typer(help="Circuit breaker: состояние и сброс.", no_args_is_help=True)
app.add_typer(cb_app, name="cb")
orders_app = typer.Typer(
    help="Ордера: список и подтверждение первого ордера MICRO_LIVE.", no_args_is_help=True
)
app.add_typer(orders_app, name="orders")
mode_app = typer.Typer(help="Режим работы: просмотр и смена (ARCH §1.4).", no_args_is_help=True)
app.add_typer(mode_app, name="mode")
news_app = typer.Typer(help="Новости: сбор, лента, реакции (ARCH §8).", no_args_is_help=True)
app.add_typer(news_app, name="news")

T = TypeVar("T")

# Значение по умолчанию берём из модели Settings без загрузки .env:
# командам БД не нужны секреты (api_auth_token и пр.).
DEFAULT_DB_PATH: str = Settings.model_fields["db_path"].default
DbOption = typer.Option(DEFAULT_DB_PATH, "--db", help="Путь к файлу SQLite.")


def version_line() -> str:
    """`augur 0.3.1 (db_schema: 2, api: 1.0)` — версии кода, без чтения БД."""
    return f"augur {__version__} (db_schema: {DB_SCHEMA_VERSION}, api: {API_VERSION})"


def _version_callback(value: bool) -> None:
    """Печатает версии приложения, ожидаемой схемы БД и контракта Fusion и завершает работу."""
    if value:
        typer.echo(version_line())
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
    typer.echo(f"strategy:          {strat.strategy_version} {params_s}")
    typer.echo(
        f"data:              {data_source} {asset} {timeframe} {start_d}..{until_d} "
        f"({len(bars)} bars)"
    )
    typer.echo(
        f"costs:             commission {commission}% | slippage {slippage}% | fill {fill_on}"
    )
    _print_result(result, cash)


# --- исполнение: kill switch, circuit breaker, ордера (0.6.0) ---------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # noqa: BLE001 — имя пользователя не критично
        return "cli:unknown"


def _open_db(db: str) -> sqlite3.Connection:
    """Соединение с БД, где применены все миграции кода; иначе — понятная ошибка."""
    if not Path(db).is_file():
        typer.echo(f"ERROR: database not found: {db} (run: augur db migrate)", err=True)
        raise typer.Exit(code=1)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except sqlite3.Error:
        row = None
    if row is None or row[0] is None or int(row[0]) < DB_SCHEMA_VERSION:
        conn.close()
        typer.echo(
            f"ERROR: schema older than {DB_SCHEMA_VERSION} (run: augur db migrate)", err=True
        )
        raise typer.Exit(code=1)
    return conn


def _try_settings() -> Settings | None:
    try:
        return load_settings()
    except ValidationError:
        return None


@app.command("kill-switch")
def kill_switch(
    flatten: bool = typer.Option(
        True, "--flatten/--no-flatten", help="Закрыть позиции рыночными reduce_only-ордерами."
    ),
    db: str = DbOption,
) -> None:
    """Аварийная остановка: KILLED, режим ANALYSIS_ONLY, отмена ордеров, закрытие позиций.

    Без кода подтверждения (ARCH §7.4). Снятие — `augur cb reset --confirm`.
    """
    conn = _open_db(db)
    try:
        settings = _try_settings()
        adapter = _live_adapter(settings) if settings is not None else None
        res = asyncio.run(
            engage_kill_switch(
                conn, settings, actor=_actor(), flatten=flatten, clock=_now_ms, live_adapter=adapter
            )
        )
    finally:
        conn.close()
    for w in res.warnings:
        typer.echo(f"WARNING: {w}", err=True)
    typer.echo(f"KILL SWITCH ENGAGED (was {res.was_state}) -> KILLED, mode ANALYSIS_ONLY")
    typer.echo(f"cancelled orders: {res.cancelled}")
    if flatten:
        typer.echo(f"flatten orders:   {res.flatten_orders}")
    typer.echo("release: augur cb reset --confirm")


def _breaker(conn: sqlite3.Connection) -> CircuitBreaker:
    settings = _try_settings()
    base = settings or Settings.model_construct()
    configured = settings.trading_mode if settings is not None else TradingMode.PAPER
    return CircuitBreaker(
        conn,
        orders=None,
        modes=ModeStore(conn, configured_mode=configured),
        limits=base.risk,
        cb=base.cb,
        fusion=base.fusion,
        clock=_now_ms,
    )


@cb_app.command("status")
def cb_status(db: str = DbOption) -> None:
    """Состояние circuit breaker и текущий режим."""
    conn = _open_db(db)
    try:
        st = _breaker(conn).status()
        mode = ModeStore(conn, configured_mode=TradingMode.FULL_AUTO).stored()
    finally:
        conn.close()
    typer.echo(f"state:      {st.state}")
    typer.echo(f"reason:     {st.reason or '-'}")
    typer.echo(f"tripped_at: {_fmt_ts(st.tripped_ts)}")
    typer.echo(f"reset_by:   {st.reset_by or '-'} at {_fmt_ts(st.reset_ts)}")
    typer.echo(f"mode:       {mode.value if mode else '-'}")


@cb_app.command("reset")
def cb_reset(
    confirm: bool = typer.Option(False, "--confirm", help="Обязательно: подтверждение сброса."),
    db: str = DbOption,
) -> None:
    """Сбросить CB/kill switch в ARMED по одноразовому коду; режим после сброса — PAPER."""
    if not confirm:
        typer.echo("ERROR: --confirm is required", err=True)
        raise typer.Exit(code=2)
    conn = _open_db(db)
    try:
        breaker = _breaker(conn)
        if breaker.state == "ARMED":
            typer.echo("circuit breaker is already ARMED")
            return
        ticket = breaker.issue_reset_code()
        typer.echo(f"state {breaker.state}: {breaker.status().reason or '-'}")
        typer.echo(f"confirmation code: {ticket.code} (valid until {_fmt_ts(ticket.expires_ts)})")
        code = typer.prompt("enter the code to reset")
        try:
            st = breaker.reset(actor=_actor(), confirm_code=code.strip())
        except InvalidResetCodeError as e:
            typer.echo(f"ERROR: {e}", err=True)
            raise typer.Exit(code=1) from e
    finally:
        conn.close()
    typer.echo(f"circuit breaker {st.state}; mode PAPER (escalation: augur mode set)")


@orders_app.command("list")
def orders_list(
    limit: int = typer.Option(20, "--limit", min=1, help="Сколько последних ордеров."),
    state: str | None = typer.Option(None, "--state", help="Фильтр по состоянию."),
    db: str = DbOption,
) -> None:
    """Последние ордера: время, состояние, сторона, объём, исполнение."""
    conn = _open_db(db)
    try:
        rows = OrderRepository(conn).recent(limit=limit, state=state.upper() if state else None)
    finally:
        conn.close()
    typer.echo(
        f"{'created':<23} {'id':<26} {'state':<9} {'mode':<6} {'symbol':<10} {'side':<4} "
        f"{'qty':>14} {'filled':>14} {'avg':>14}"
    )
    for o in rows:
        typer.echo(
            f"{_fmt_ts(o.created_ts):<23} {o.id:<26} {o.state.value:<9} {o.mode:<6} "
            f"{o.symbol:<10} {o.side:<4} {o.qty:>14} {o.filled_qty:>14} "
            f"{o.avg_fill_price if o.avg_fill_price is not None else '-':>14}"
        )


# --- MICRO_LIVE: режим, подтверждение первого ордера, сверка (0.7.0) ------------------


def _live_adapter(settings: Settings) -> MarketAdapter | None:
    """Реальный адаптер для live-стека; None — CcxtAdapter из настроек. Подменяется в тестах."""
    return None


def _live_allowed(settings: Settings) -> bool:
    return live_allowed(settings)


def _require_settings() -> Settings:
    try:
        return load_settings()
    except ValidationError as e:
        typer.echo(f"ERROR: settings invalid ({e.error_count()} errors)", err=True)
        raise typer.Exit(code=1) from e


async def _with_live(
    conn: sqlite3.Connection, settings: Settings, fn: Callable[[LiveStack], Awaitable[T]]
) -> T:
    """Собрать live-стек, подключиться к площадке, выполнить `fn`, отключиться."""
    try:
        stack = build_live_stack(
            conn,
            settings,
            app_version=__version__,
            clock=_now_ms,
            adapter=_live_adapter(settings),
        )
    except ExecutionRefusedError as e:
        typer.echo(f"ERROR: {e}", err=True)
        raise typer.Exit(code=1) from e
    venue = stack.orders.venue
    net = "TESTNET" if settings.ccxt_sandbox else "REAL MONEY"
    typer.echo(f"venue: {venue} ({net})")
    await stack.venue_adapter.connect()
    try:
        return await fn(stack)
    finally:
        await stack.venue_adapter.close()


ModeArg = typer.Argument(..., help="Целевой режим.")
OrderIdArg = typer.Argument(..., help="id ордера (augur mode show / orders list).")


@mode_app.command("show")
def mode_show(db: str = DbOption) -> None:
    """Текущий режим, потолки конфига, CB, подтверждение первого ордера, promotion."""
    conn = _open_db(db)
    try:
        settings = _require_settings()
        ctl = ModeController(conn, settings=settings, clock=_now_ms)
        stored = ctl.store.stored()
        promo = ctl.promotion_status()
        cb = _breaker(conn).status()
        confirmed = first_order_confirmed(conn)
        waiting = [
            o.id
            for o in OrderRepository(conn).active(mode=TradingMode.MICRO_LIVE.value)
            if awaiting_approval(conn, o.id)
        ]
    finally:
        conn.close()
    typer.echo(f"mode:             {ctl_mode_line(stored, settings)}")
    typer.echo(f"trading_mode:     {settings.trading_mode.value}")
    typer.echo(f"max_allowed_mode: {settings.max_allowed_mode.value}")
    typer.echo(f"circuit breaker:  {cb.state}")
    typer.echo(f"micro 1st order:  {'confirmed' if confirmed else 'awaits approval'}")
    typer.echo(f"awaiting orders:  {', '.join(waiting) or '-'}")
    typer.echo(
        f"FULL_AUTO promo:  {promo.micro_trades}/{promo.required} trades, "
        f"cb_tripped_since_entry={promo.cb_tripped_since_entry}"
    )


def ctl_mode_line(stored: TradingMode | None, settings: Settings) -> str:
    effective = ModeStore.effective(stored, settings.trading_mode)
    note = "" if stored == effective else f" (stored {stored.value if stored else '-'})"
    return f"{effective.value}{note}"


@mode_app.command("set")
def mode_set(
    target: TradingMode = ModeArg,
    confirm: bool = typer.Option(False, "--confirm", help="Обязательно: подтверждение смены."),
    reason: str = typer.Option("manual", "--reason", help="Причина (для понижения)."),
    db: str = DbOption,
) -> None:
    """Сменить режим. Повышение — на один шаг, с кодом для MICRO_LIVE/FULL_AUTO; понижение — сразу.

    Вход в MICRO_LIVE: сначала сверка позиций `settings.symbols` с реальной площадкой.
    """
    if not confirm:
        typer.echo("ERROR: --confirm is required", err=True)
        raise typer.Exit(code=2)
    settings = _require_settings()
    conn = _open_db(db)
    try:
        ctl = ModeController(conn, settings=settings, clock=_now_ms)
        current = ctl.current()
        if target == current:
            typer.echo(f"mode is already {current.value}")
            return
        try:
            if mode_rank(target) < mode_rank(current):
                ctl.downgrade(target, actor=_actor(), reason=reason)
            elif target == TradingMode.MICRO_LIVE:
                asyncio.run(_with_live(conn, settings, lambda st: _escalate(ctl, target, st)))
            else:
                asyncio.run(_escalate(ctl, target, None))
        except ModeChangeError as e:
            typer.echo(f"ERROR: {e}", err=True)
            raise typer.Exit(code=1) from e
        new = ctl.current()
    finally:
        conn.close()
    typer.echo(f"mode {current.value} -> {new.value}")
    if new == TradingMode.MICRO_LIVE:
        typer.echo("first MICRO_LIVE order will wait for: augur orders approve <id> --confirm")


async def _escalate(ctl: ModeController, target: TradingMode, stack: LiveStack | None) -> None:
    ctl.check_escalation(target)
    if stack is not None:
        mismatches = await stack.reconciler.check_positions()
        if mismatches:
            for m in mismatches:
                typer.echo(f"  {m.symbol}: db={m.db_qty} venue={m.venue_qty}", err=True)
            raise ModeChangeError(
                "positions at the venue differ from the DB: use a dedicated subaccount with no "
                f"base assets for {', '.join(sorted({m.symbol for m in mismatches}))}"
            )
    ticket = await ctl.request_change(target, actor=_actor())
    code = ticket.code
    if ticket.code_required:
        typer.echo(f"{ticket.source.value} -> {ticket.target.value}")
        typer.echo(f"confirmation code: {ticket.code} (valid until {_fmt_ts(ticket.expires_ts)})")
        code = typer.prompt("enter the code to confirm").strip()
    ctl.confirm_change(ticket.ticket_id, code, actor=_actor())


@orders_app.command("approve")
def orders_approve(
    order_id: str = OrderIdArg,
    confirm: bool = typer.Option(False, "--confirm", help="Обязательно: подтверждение отправки."),
    db: str = DbOption,
) -> None:
    """Подтвердить первый ордер MICRO_LIVE кодом и отправить его на площадку (ARCH §7.1)."""
    if not confirm:
        typer.echo("ERROR: --confirm is required", err=True)
        raise typer.Exit(code=2)
    settings = _require_settings()
    conn = _open_db(db)
    try:

        async def run(stack: LiveStack) -> Any:
            ticket = stack.approval.issue_code(order_id)
            o = stack.orders.orders.get(order_id)
            assert o is not None  # noqa: S101 — проверено issue_code
            typer.echo(f"order {o.id}: {o.side} {o.qty} {o.symbol} (coid {o.client_order_id})")
            typer.echo(
                f"confirmation code: {ticket.code} (valid until {_fmt_ts(ticket.expires_ts)})"
            )
            code = typer.prompt("enter the code to send the order").strip()
            return await stack.approval.approve(order_id, code, actor=_actor())

        try:
            rec = asyncio.run(_with_live(conn, settings, run))
        except ApprovalError as e:
            typer.echo(f"ERROR: {e}", err=True)
            raise typer.Exit(code=1) from e
    finally:
        conn.close()
    typer.echo(f"order {rec.id}: {rec.state.value}, filled {rec.filled_qty}")


@app.command("reconcile")
def reconcile(db: str = DbOption) -> None:
    """Сверить ордера и позиции БД с реальной площадкой (MICRO_LIVE). Расхождение -> CB."""
    settings = _require_settings()
    if not _live_allowed(settings):
        typer.echo("ERROR: reconcile needs a config that allows MICRO_LIVE", err=True)
        raise typer.Exit(code=1)
    conn = _open_db(db)
    try:
        report = asyncio.run(_with_live(conn, settings, lambda st: st.reconciler.run()))
    finally:
        conn.close()
    typer.echo(f"checked orders:    {report.checked_orders}")
    typer.echo(f"updated orders:    {', '.join(report.updated_orders) or '-'}")
    typer.echo(f"unresolved orders: {', '.join(report.unresolved_orders) or '-'}")
    for m in report.mismatches:
        typer.echo(f"MISMATCH {m.symbol}: db={m.db_qty} venue={m.venue_qty} tol={m.tolerance}")
    if not report.ok:
        raise typer.Exit(code=1)


# --- News (0.8.0, ARCH §8, §9.2) --------------------------------------------------------


def _news_fetchers(settings: Settings) -> list[NewsFetcher] | None:
    """Источники для `news fetch`; None — Finnhub из настроек. Подменяется в тестах."""
    return None


def _news_scorer(conn: sqlite3.Connection, settings: Settings) -> Any:
    """`news.scorer=llm` → LLM-скорер Fusion с fallback на rule:v1 (0.10.0); иначе None."""
    if settings.news.scorer != "llm":
        return None
    from augur.fusion.factory import llm_keys
    from augur.fusion.llm.client import OpenAICompatClient
    from augur.fusion.llm.news_scorer import LLMNewsScorer
    from augur.news import RuleBasedScorer

    return LLMNewsScorer(
        conn,
        settings.fusion.llm,
        OpenAICompatClient(),
        llm_keys(settings),
        fallback=RuleBasedScorer(settings.news.source_weights),
        clock=_now_ms,
    )


@news_app.command("fetch")
def news_fetch(db: str = DbOption) -> None:
    """Один цикл: Finnhub -> нормализация -> дедуп -> rule:v1 -> news -> реакции."""
    settings = _require_settings()
    conn = _open_db(db)
    try:
        try:
            pipeline = build_news_pipeline(
                conn,
                settings,
                fetchers=_news_fetchers(settings),
                scorer=_news_scorer(conn, settings),
            )
        except ValueError as e:
            typer.echo(f"ERROR: {e}", err=True)
            raise typer.Exit(code=1) from e
        result = asyncio.run(pipeline.run_once())
    finally:
        conn.close()
    typer.echo(
        f"fetched {result.fetched}, inserted {result.inserted}, "
        f"duplicates {result.duplicates}, dropped {result.dropped}"
    )
    for ev in result.events:
        typer.echo(
            f"  {ev.importance or '-':<6} {ev.sentiment:+.2f} "
            f"{','.join(ev.symbols) or '-':<16} {(ev.title or '')[:80]}"
        )
    for r in result.reactions:
        target = r.symbol or r.strategy or "-"
        until = _fmt_ts(r.expires_ts) if r.expires_ts is not None else "-"
        typer.echo(f"REACTION {r.rule}: {r.action} {target} until {until}")
    for source, err in result.errors.items():
        typer.echo(f"ERROR {source}: {err}", err=True)
    if result.errors and len(result.errors) == len(pipeline.sources):
        raise typer.Exit(code=1)  # ни один источник не ответил


@news_app.command("list")
def news_list(
    symbol: str | None = typer.Option(None, "--symbol", help="Фильтр по символу (BTC/USDT)."),
    limit: int = typer.Option(20, "--limit", min=1, help="Сколько последних новостей."),
    db: str = DbOption,
) -> None:
    """Последние новости: время публикации, importance, sentiment, символы, заголовок."""
    conn = _open_db(db)
    try:
        rows = NewsRepository(conn).recent(symbol=symbol, limit=limit)
    finally:
        conn.close()
    typer.echo(f"{'published':<23} {'imp':<6} {'sent':>6} {'symbols':<16} title")
    for n in rows:
        sent = f"{n.sentiment:+.2f}" if n.sentiment is not None else "-"
        typer.echo(
            f"{_fmt_ts(n.published_ts):<23} {n.importance or '-':<6} {sent:>6} "
            f"{','.join(n.symbols) or '-':<16} {n.title[:80]}"
        )


@news_app.command("reactions")
def news_reactions(db: str = DbOption) -> None:
    """Активные реакции: паузы символов/стратегий (R16) и news-флаги (R15, soft)."""
    conn = _open_db(db)
    try:
        rows = NewsReactionRules(conn, []).active(now_ms=_now_ms())
    finally:
        conn.close()
    if not rows:
        typer.echo("no active news reactions")
        return
    for r in rows:
        target = r.symbol or r.strategy or "-"
        flag = f" flag={r.flag_code}:{r.flag_severity}" if r.flag_code else ""
        typer.echo(
            f"{r.id} {r.rule}: {r.action} {target}{flag} until {_fmt_ts(r.expires_ts)} "
            f"(news {r.news_id})"
        )


@news_app.command("lift")
def news_lift(
    reaction_id: str = typer.Argument(..., help="id реакции из `augur news reactions`."),
    confirm: bool = typer.Option(False, "--confirm", help="Обязательно: подтверждение снятия."),
    db: str = DbOption,
) -> None:
    """Снять паузу или news-флаг досрочно (с записью NEWS_REACTION_LIFTED в аудит)."""
    if not confirm:
        typer.echo("ERROR: --confirm is required", err=True)
        raise typer.Exit(code=2)
    conn = _open_db(db)
    try:
        rec = NewsReactionRules(conn, []).lift(reaction_id, actor=_actor(), now_ms=_now_ms())
    except ReactionNotFoundError as e:
        typer.echo(f"ERROR: no active reaction {reaction_id}", err=True)
        raise typer.Exit(code=1) from e
    finally:
        conn.close()
    typer.echo(f"lifted {rec.id}: {rec.action} {rec.symbol or rec.strategy or '-'}")


# --- Сигналы и аудит: decision chain, hash-chain (0.9.0, ARCH §7.5, §9.2) ----------------

signals_app = typer.Typer(help="Сигналы: лента и цепочка решения.", no_args_is_help=True)
app.add_typer(signals_app, name="signals")
audit_app = typer.Typer(help="Аудит: цепочка решения, проверка hash-chain.", no_args_is_help=True)
app.add_typer(audit_app, name="audit")


def _parse_since(value: str | None) -> int | None:
    """ISO-дата/время (`2026-11-27`, `2026-11-27T10:00`) → Unix ms; без зоны — UTC."""
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        typer.echo(f"ERROR: --since: expected ISO date/time, got {value!r}", err=True)
        raise typer.Exit(code=2) from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


@signals_app.command("list")
def signals_list(
    symbol: str | None = typer.Option(None, "--symbol", help="Фильтр по символу (BTC/USDT)."),
    since: str | None = typer.Option(None, "--since", help="Не раньше (ISO, UTC)."),
    kind: str | None = typer.Option(None, "--kind", help="strategy | final."),
    limit: int = typer.Option(20, "--limit", min=1, help="Сколько последних сигналов."),
    db: str = DbOption,
) -> None:
    """Последние сигналы: время, вид, источник, действие, сила, статус."""
    since_ms = _parse_since(since)
    if kind is not None and kind not in ("strategy", "final"):
        typer.echo("ERROR: --kind must be 'strategy' or 'final'", err=True)
        raise typer.Exit(code=2)
    conn = _open_db(db)
    try:
        rows = ReadService(conn).signals(symbol=symbol, since=since_ms, kind=kind, limit=limit)
    finally:
        conn.close()
    typer.echo(
        f"{'created':<23} {'id':<26} {'kind':<8} {'source':<16} {'symbol':<10} {'action':<9} "
        f"{'str':>5} {'status':<12} strategy"
    )
    for s in rows:
        typer.echo(
            f"{_fmt_ts(s.created_ts):<23} {s.id:<26} {s.kind:<8} {s.source:<16} {s.symbol:<10} "
            f"{s.action:<9} {s.strength:>5.2f} {s.status:<12} {s.strategy or '-'}"
        )


def _print_chain(chain: DecisionChainDTO) -> None:
    s = chain.signal
    typer.echo(f"signal {s.id} [{s.kind}] {s.symbol} {s.action} strength={s.strength:.2f}")
    typer.echo(f"  source={s.source} status={s.status} created={_fmt_ts(s.created_ts)}")
    typer.echo(f"  expires={_fmt_ts(s.expires_ts)} config_hash={s.config_hash[:12]}…")
    typer.echo(f"  code_version={s.code_version} risk_flags={s.risk_flags or '-'}")
    for st in chain.strategy_signals:
        typer.echo(
            f"  <- strategy {st.strategy or '-'} {st.action} {st.strength:.2f} "
            f"bar={_fmt_ts(st.bar_ts)} data_hash={(st.data_hash or '-')[:12]} id={st.id}"
        )
    f = chain.fused_signal
    if f is None:
        typer.echo("fused_signal: - (fusion not used)")
    else:
        typer.echo(
            f"fused_signal {f.id} {f.action} {f.strength:.2f} valid={f.valid} "
            f"api={f.api_version} cfg={f.fusion_config_version}"
        )
        typer.echo(f"  reason: {f.reason}")
    if not chain.decisions:
        typer.echo("decision_audit: - (no risk decision)")
    for d in chain.decisions:
        typer.echo(
            f"decision {d.id} {d.risk_verdict} mode={d.mode} reason={d.risk_reason or '-'} "
            f"at {_fmt_ts(d.created_at)}"
        )
        typer.echo(
            f"  versions: app={d.app_version} strategy={d.strategy_version} "
            f"ml={d.ml_model_version} prompt={d.llm_prompt_version}"
        )
        if d.execution_result is not None:
            typer.echo(f"  execution_result: {json.dumps(d.execution_result, ensure_ascii=False)}")
    for o in chain.orders:
        typer.echo(
            f"order {o.id} {o.state} {o.side} {o.qty} {o.symbol} @ {o.avg_fill_price or '-'} "
            f"filled={o.filled_qty} fee={o.fee} mode={o.mode} coid={o.client_order_id}"
        )
        if o.reject_reason:
            typer.echo(f"  reject_reason: {o.reject_reason}")
        for fl in o.fills:
            typer.echo(f"  fill {fl.qty} @ {fl.price} fee={fl.fee} at {_fmt_ts(fl.ts)}")
    for p in chain.positions:
        typer.echo(f"position {p.venue}/{p.symbol}/{p.mode} qty={p.qty} avg={p.avg_price}")
    if chain.events:
        typer.echo("events:")
    for e in chain.events:
        typer.echo(f"  {_fmt_ts(e.ts)} #{e.id} {e.event:<28} {e.severity:<8} {e.actor}")


def _explain(ref_id: str, db: str, as_json: bool) -> None:
    conn = _open_db(db)
    try:
        chain = ReadService(conn).decision_chain(ref_id)
    except ChainNotFoundError as e:
        typer.echo(f"ERROR: no signal or order with id {ref_id}", err=True)
        raise typer.Exit(code=1) from e
    finally:
        conn.close()
    if as_json:
        typer.echo(chain.model_dump_json(indent=2))
    else:
        _print_chain(chain)


RefIdArg = typer.Argument(..., help="id сигнала (strategy/final) или ордера.")
JsonOption = typer.Option(False, "--json", help="Вывести DecisionChainDTO в JSON.")


@signals_app.command("explain")
def signals_explain(ref_id: str = RefIdArg, as_json: bool = JsonOption, db: str = DbOption) -> None:
    """Цепочка решения: сигналы -> fused -> decision_audit -> ордера -> fills -> события."""
    _explain(ref_id, db, as_json)


@audit_app.command("explain")
def audit_explain(ref_id: str = RefIdArg, as_json: bool = JsonOption, db: str = DbOption) -> None:
    """То же, что `signals explain` (ARCH §7.5): цепочка решения по сигналу или ордеру."""
    _explain(ref_id, db, as_json)


@audit_app.command("verify-chain")
def audit_verify_chain(db: str = DbOption) -> None:
    """Проверка hash-chain `audit_log`: prev_hash и row_hash каждой строки."""
    conn = _open_db(db)
    try:
        rep = verify_audit_chain(conn)
    finally:
        conn.close()
    if rep.ok:
        typer.echo(f"audit_log hash-chain OK ({rep.rows_checked} rows)")
        return
    typer.echo(
        f"ERROR: audit_log hash-chain BROKEN at id {rep.first_bad_id}: {rep.problem} "
        f"(checked {rep.rows_checked} rows)",
        err=True,
    )
    raise typer.Exit(code=1)


# --- GUI мониторинга и REST (0.9.0, ARCH §9.1) -------------------------------------------

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@app.command("gui")
def gui(
    host: str = typer.Option(
        "127.0.0.1", "--host", help="Адрес. Наружу — только через TLS-прокси."
    ),
    port: int = typer.Option(8000, "--port", min=1, max=65535),
    poll: float = typer.Option(2.0, "--poll", min=0.2, help="Период опроса БД для /ws/events, с."),
    db: str | None = typer.Option(None, "--db", help="Путь к SQLite (по умолчанию DB_PATH)."),
) -> None:
    """GUI мониторинга + REST `/api/v1` + WebSocket `/ws/events` (только чтение и kill switch)."""
    import uvicorn

    from augur.interfaces.api import create_app

    settings = _require_settings()
    db_path = db or settings.db_path
    _open_db(db_path).close()  # схема актуальна — иначе понятная ошибка до старта сервера
    if host not in LOOPBACK_HOSTS:
        typer.echo(
            f"WARNING: listening on {host}: expose only behind a TLS reverse proxy (ARCH §9.1)",
            err=True,
        )
    api = create_app(settings, db_path=db_path, ws_poll_s=poll, live_adapter=_live_adapter)
    typer.echo(f"Augur GUI: http://{host}:{port}/  (token: API_AUTH_TOKEN)")
    uvicorn.run(api, host=host, port=port, log_level="info")


# --- ML / LLM / Fusion (0.10.0; ML_LLM_FUSION_SPEC.md §2.4, §3, §5.5) ---------------------

ml_app = typer.Typer(help="ML-модели: обучение, реестр, промоушен, replay.", no_args_is_help=True)
app.add_typer(ml_app, name="ml")
llm_app = typer.Typer(help="LLM-контекст: обновление и расход.", no_args_is_help=True)
app.add_typer(llm_app, name="llm")
fusion_app = typer.Typer(help="Fusion: воспроизведение решений.", no_args_is_help=True)
app.add_typer(fusion_app, name="fusion")

SymbolOpt = typer.Option(..., "--symbol", help="Инструмент, напр. BTC/USDT.")
TimeframeOpt = typer.Option("1h", "--timeframe", help="Таймфрейм баров.")


def _ml_settings_or_default() -> Settings:
    settings = _try_settings()
    if settings is None:
        typer.echo("ERROR: settings invalid (API_AUTH_TOKEN и пр. в .env)", err=True)
        raise typer.Exit(code=1)
    return settings


@ml_app.command("train")
def ml_train(
    symbol: str = SymbolOpt,
    timeframe: str = TimeframeOpt,
    since: str | None = typer.Option(None, "--since", help="Начало истории, YYYY-MM-DD."),
    db: str = DbOption,
) -> None:
    """Walk-forward + итоговая модель → артефакт lightgbm_v<N> → реестр (candidate)."""
    from augur.backtest.costs import CostModel
    from augur.fusion.ml.data import load_bar_arrays
    from augur.fusion.ml.registry import ModelRegistry, model_name
    from augur.fusion.ml.train import train_model

    settings = _ml_settings_or_default()
    cfg = settings.fusion.ml
    conn = _open_db(db)
    try:
        since_ms = _to_ms(_parse_date(since, "--since")) if since else None
        bars = load_bar_arrays(
            conn, venue=cfg.venue, symbol=symbol, timeframe=timeframe, since_open=since_ms
        )
        if bars is None:
            typer.echo(f"ERROR: no bars in ohlcv for {cfg.venue} {symbol} {timeframe}", err=True)
            raise typer.Exit(code=1)
        registry = ModelRegistry(conn, clock=_now_ms)
        version = registry.next_version()
        name = model_name(symbol, timeframe)
        try:
            res = train_model(
                bars,
                asset=symbol,
                name=name,
                version=version,
                cfg=cfg,
                min_confidence=settings.fusion.config.ml_min_confidence,
                costs=CostModel(
                    commission_pct=settings.execution.commission_pct,
                    slippage_pct=settings.execution.slippage_pct,
                ),
                artifacts_dir=Path(cfg.artifacts_dir),
            )
        except (ValueError, ImportError) as e:
            typer.echo(f"ERROR: {e}", err=True)
            raise typer.Exit(code=1) from e
        rec = registry.register(
            name=name, version=version, artifact_path=res.path, trained_at=_now_ms(), actor=_actor()
        )
    finally:
        conn.close()
    typer.echo(f"{rec.version} {rec.name} status={rec.status} sha256={rec.artifact_sha256[:12]}")
    for k, v in res.metrics.items():
        typer.echo(f"  {k:<14} {v}")


@ml_app.command("list")
def ml_list(db: str = DbOption) -> None:
    """Модели реестра: версия, имя, статус, ECE и Sharpe walk-forward."""
    from augur.fusion.ml.registry import ModelRegistry

    conn = _open_db(db)
    try:
        recs = ModelRegistry(conn, clock=_now_ms).list()
    finally:
        conn.close()
    if not recs:
        typer.echo("no models")
    for r in recs:
        typer.echo(
            f"{r.version:<14} {r.name:<28} {r.status:<10} ece={r.metrics.get('ece')} "
            f"sharpe={r.metrics.get('sharpe')} since={_fmt_ts(r.status_changed_at)} "
            f"by={r.approved_by or '-'}"
        )


@ml_app.command("promote")
def ml_promote(
    version: str = typer.Argument(..., help="lightgbm_v<N>"),
    to: str = typer.Option(..., "--to", help="staging | production | retired"),
    approved_by: str = typer.Option(..., "--approved-by", help="Кто подтверждает."),
    confirm: bool = typer.Option(False, "--confirm", help="Подтвердить смену статуса."),
    db: str = DbOption,
) -> None:
    """Смена статуса модели по §2.4 (автопромоушена нет)."""
    from augur.fusion.ml.registry import ModelRegistry, PromotionError

    if to not in ("staging", "production", "retired"):
        typer.echo("ERROR: --to must be staging | production | retired", err=True)
        raise typer.Exit(code=2)
    if not confirm:
        typer.echo("ERROR: add --confirm to change model status", err=True)
        raise typer.Exit(code=1)
    settings = _ml_settings_or_default()
    conn = _open_db(db)
    try:
        rec = ModelRegistry(conn, clock=_now_ms).promote(
            version,
            to,  # type: ignore[arg-type]
            approved_by=approved_by,
            rules=settings.fusion.ml.promotion,
        )
    except (PromotionError, KeyError) as e:
        typer.echo(f"ERROR: {e}", err=True)
        raise typer.Exit(code=1) from e
    finally:
        conn.close()
    typer.echo(f"{rec.version} → {rec.status} (approved_by={rec.approved_by})")


@ml_app.command("replay")
def ml_replay(prediction_id: str = typer.Argument(...), db: str = DbOption) -> None:
    """Пересчитать прогноз из `ml_predictions` по model_version и барам."""
    from augur.fusion.ml.data import load_bar_arrays
    from augur.fusion.ml.inference import (
        MLService,
        PredictionMismatch,
        load_prediction,
        replay_prediction,
    )
    from augur.fusion.ml.registry import ModelRegistry, ModelUnavailable

    settings = _ml_settings_or_default()
    cfg = settings.fusion.ml
    conn = _open_db(db)
    try:
        try:
            stored = load_prediction(conn, prediction_id)
        except KeyError as e:
            typer.echo(f"ERROR: prediction not found: {prediction_id}", err=True)
            raise typer.Exit(code=1) from e
        registry = ModelRegistry(conn, clock=_now_ms)
        meta_tf = registry.by_version(stored.model_version).name.rsplit("_", 1)[-1]
        bars = load_bar_arrays(
            conn,
            venue=cfg.venue,
            symbol=stored.asset,
            timeframe=meta_tf,
            until_close=stored.timestamp,
            limit=cfg.features.window,
        )
        if bars is None:
            typer.echo("ERROR: bars not found", err=True)
            raise typer.Exit(code=1)
        try:
            replay_prediction(
                conn, MLService(conn, registry, cfg, clock=_now_ms), registry, prediction_id, bars
            )
        except (PredictionMismatch, ModelUnavailable) as e:
            typer.echo(f"MISMATCH: {e}", err=True)
            raise typer.Exit(code=1) from e
    finally:
        conn.close()
    typer.echo(
        f"OK {prediction_id}: {stored.direction} {stored.confidence:.4f} {stored.input_hash}"
    )


@llm_app.command("refresh")
def llm_refresh(
    symbol: str = SymbolOpt,
    timeframe: str = TimeframeOpt,
    event: bool = typer.Option(False, "--event", help="Вызов по событию (бюджет ≥ 80 %)."),
    db: str = DbOption,
) -> None:
    """Один цикл LLM-контекста по активу: новости + сводка цены → llm_contexts."""
    from augur.fusion.factory import build_llm_context_service
    from augur.fusion.llm.inputs import load_news_inputs, load_price_summary

    settings = _require_settings()
    lcfg = settings.fusion.llm
    conn = _open_db(db)
    try:
        now = _now_ms()
        news = load_news_inputs(
            conn,
            asset=symbol,
            as_of=now,
            window_h=lcfg.news_window_h,
            limit=lcfg.max_news,
            body_chars=lcfg.news_body_chars,
        )
        price = load_price_summary(
            conn,
            venue=settings.fusion.ml.venue,
            asset=symbol,
            timeframe=timeframe,
            as_of=now,
            window_h=lcfg.news_window_h,
        )
        svc = build_llm_context_service(conn, settings, clock=_now_ms)
        res = asyncio.run(
            svc.refresh(symbol, as_of=now, news=news, price=price, event_driven=event)
        )
    finally:
        conn.close()
    typer.echo(f"{res.status} reason={res.reject_reason or '-'} calls={res.llm_call_ids}")
    if res.context is not None:
        c = res.context
        flags = ", ".join(f"{f.code.value}:{f.severity.value}" for f in c.risk_flags) or "-"
        typer.echo(
            f"  context_score={c.context_score:+.3f} confidence={c.confidence:.2f} flags={flags}"
        )


@llm_app.command("usage")
def llm_usage(db: str = DbOption) -> None:
    """Расход LLM за текущие сутки UTC по провайдерам (FUSION SPEC §7)."""
    conn = _open_db(db)
    try:
        now = _now_ms()
        rows = conn.execute(
            "SELECT provider, task, COUNT(*), COALESCE(SUM(cost_usd), 0), "
            "COALESCE(AVG(latency_ms), 0), SUM(status <> 'ok') FROM llm_calls "
            "WHERE created_at >= ? GROUP BY provider, task ORDER BY provider, task",
            (now - now % 86_400_000,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        typer.echo("no LLM calls today")
    for p, task, n, usd, avg, bad in rows:
        typer.echo(f"{p:<12} {task:<15} calls={n} usd={usd:.4f} avg_ms={avg:.0f} not_ok={bad}")


@fusion_app.command("replay")
def fusion_replay(
    ref_id: str = typer.Argument(..., help="id fused_signal / decision_audit."),
    as_json: bool = JsonOption,
    db: str = DbOption,
) -> None:
    """`fuse()` на сохранённых входах; расхождение с записанным → код выхода 1."""
    from augur.fusion.replay import ReplayError, replay_fused

    settings = _ml_settings_or_default()
    conn = _open_db(db)
    try:
        res = replay_fused(conn, ref_id, settings.fusion.config)
    except ReplayError as e:
        typer.echo(f"ERROR: {e}", err=True)
        raise typer.Exit(code=1) from e
    finally:
        conn.close()
    if as_json:
        typer.echo(res.model_dump_json(indent=2))
    else:
        typer.echo(f"{'MATCH' if res.match else 'MISMATCH'} {res.fused_signal_id}")
        for f in res.diffs:
            typer.echo(f"  {f}: recorded={res.recorded[f]!r} replayed={res.replayed[f]!r}")
    if not res.match:
        raise typer.Exit(code=1)
