"""`augur --version`: версия из pyproject.toml + db_schema и api из augur.versions."""

import subprocess
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path

from typer.testing import CliRunner

import augur
from augur.interfaces.cli.main import app
from augur.versions import API_VERSION, DB_SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]


def _expected_line() -> str:
    return f"augur {_pyproject_version()} (db_schema: {DB_SCHEMA_VERSION}, api: {API_VERSION})"


def _pyproject_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as f:
        return str(tomllib.load(f)["project"]["version"])


def test_package_version_matches_pyproject() -> None:
    assert augur.__version__ == version("augur") == _pyproject_version()


def test_cli_version_option() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == _expected_line()


def test_cli_version_via_module_entrypoint() -> None:
    code = "from augur.interfaces.cli.main import app; app()"
    result = subprocess.run(  # noqa: S603 — фиксированная команда, без пользовательского ввода
        [sys.executable, "-c", code, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == _expected_line()
