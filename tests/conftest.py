"""Общие фикстуры: БД с применёнными миграциями, тестовые Settings без .env."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from augur.config.settings import Settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "src" / "augur" / "db" / "migrations"


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Применяет все NNNN_*.sql по порядку (тестовый помощник; мигратор — отдельная задача)."""
    applied = []
    for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        conn.executescript(path.read_text(encoding="utf-8"))
        applied.append(path.stem)
    return applied


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn)
    yield conn
    conn.close()


def make_settings(**overrides: object) -> Settings:
    """Settings только из аргументов: .env и окружение не читаются."""
    values: dict[str, object] = {"api_auth_token": SecretStr("test-token")}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]
