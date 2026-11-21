"""Точка входа CLI `augur` (pyproject: `augur.interfaces.cli.main:app`)."""

import typer

from augur import __version__

app = typer.Typer(name="augur", help="Augur — гибридная торговая система ML+LLM.")


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
