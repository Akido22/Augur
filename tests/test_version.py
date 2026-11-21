"""`augur --version` печатает версию из pyproject.toml (через importlib.metadata)."""

import subprocess
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path

from typer.testing import CliRunner

import augur
from augur.interfaces.cli.main import app

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as f:
        return str(tomllib.load(f)["project"]["version"])


def test_package_version_matches_pyproject() -> None:
    assert augur.__version__ == version("augur") == _pyproject_version()


def test_cli_version_option() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"augur {_pyproject_version()}"


def test_cli_version_via_module_entrypoint() -> None:
    code = "from augur.interfaces.cli.main import app; app()"
    result = subprocess.run(  # noqa: S603 — фиксированная команда, без пользовательского ввода
        [sys.executable, "-c", code, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"augur {_pyproject_version()}"
