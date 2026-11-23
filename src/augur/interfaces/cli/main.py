"""Точка входа CLI `augur` (pyproject: `augur.interfaces.cli.main:app`)."""

from datetime import UTC, datetime
from pathlib import Path

import typer

from augur import __version__
from augur.config.settings import Settings
from augur.db.migrator import MigrationError, Migrator

app = typer.Typer(name="augur", help="Augur — гибридная торговая система ML+LLM.")
db_app = typer.Typer(help="Схема SQLite: миграции и бэкапы.", no_args_is_help=True)
app.add_typer(db_app, name="db")

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
