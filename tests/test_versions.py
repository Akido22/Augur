"""augur.versions — единый источник версий; SchemaVersionMismatchError в миграторе."""

import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from augur.db.migrator import (
    MIGRATIONS_DIR,
    Migrator,
    SchemaVersionMismatchError,
    UnknownAppliedMigrationError,
)
from augur.versions import API_VERSION, DB_SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]


def test_db_schema_version_matches_latest_migration_file() -> None:
    """Появилась новая NNNN_*.sql — тест падает: обновите DB_SCHEMA_VERSION."""
    files = list((ROOT / "src" / "augur" / "db" / "migrations").glob("*.sql"))
    assert files
    assert max(int(f.name[:4]) for f in files) == DB_SCHEMA_VERSION


def test_api_version_format() -> None:
    assert re.fullmatch(r"\d+\.\d+", API_VERSION)


def test_default_migrator_expects_code_schema(tmp_path: Path) -> None:
    db = str(tmp_path / "augur.db")
    result = Migrator(backup_dir=tmp_path / "b", app_version="0.3.1").migrate(db)
    assert result.schema_version == DB_SCHEMA_VERSION


@pytest.fixture
def migrations(tmp_path: Path) -> Path:
    target = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, target)
    return target


def _migrator(migrations: Path, tmp_path: Path) -> Migrator:
    return Migrator(migrations, backup_dir=tmp_path / "backups", app_version="0.3.1")


def _versions(db: str) -> list[int]:
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
    finally:
        conn.close()


def test_unapplied_newer_migration_on_disk_raises(migrations: Path, tmp_path: Path) -> None:
    db = str(tmp_path / "augur.db")
    _migrator(migrations, tmp_path).migrate(db)
    (migrations / "0009_future.sql").write_text(
        "-- compat: expand\nCREATE TABLE future (x INTEGER) STRICT;\n", encoding="utf-8"
    )
    with pytest.raises(SchemaVersionMismatchError, match="0009_future"):
        _migrator(migrations, tmp_path).migrate(db)
    assert _versions(db) == [1, 2, 3, 4, 5, 6, 7, 8]  # отказ до изменения БД


def test_db_newer_than_code_contract_raises(migrations: Path, tmp_path: Path) -> None:
    db = str(tmp_path / "augur.db")
    m = _migrator(migrations, tmp_path)
    m.migrate(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO schema_migrations(version, name, checksum, compat, app_version) "
        "VALUES (9, '0009_x', 'x', 'expand', '9.9.9')"
    )
    conn.execute("UPDATE schema_migrations SET version=999, compat='contract' WHERE version=9")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaVersionMismatchError, match=r"DB schema 999 > code expects 8\."):
        m.migrate(db)


def test_db_newer_than_code_expand_is_compatible(migrations: Path, tmp_path: Path) -> None:
    """VERSIONING §2.2: лишние expand-миграции в БД не мешают старту старого кода."""
    db = str(tmp_path / "augur.db")
    m = _migrator(migrations, tmp_path)
    m.migrate(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO schema_migrations(version, name, checksum, compat, app_version) "
        "VALUES (999, '0999_x', 'x', 'expand', '9.9.9')"
    )
    conn.commit()
    conn.close()
    result = m.migrate(db)
    assert result.applied == [] and result.schema_version == 999


def test_db_older_than_code_raises(migrations: Path, tmp_path: Path) -> None:
    """Код ожидает схему 9, а файла 0009 в пакете нет — схема после migrate остаётся 8."""
    db = str(tmp_path / "augur.db")
    m = Migrator(migrations, backup_dir=tmp_path / "b", app_version="0.3.1", expected_schema=9)
    with pytest.raises(SchemaVersionMismatchError, match=r"DB schema 8 < expected 9\."):
        m.migrate(db)
    with pytest.raises(SchemaVersionMismatchError, match="augur db migrate"):
        m.migrate(db)  # повторный запуск без новых миграций — та же ошибка


def test_missing_contract_migration_below_expected_still_detected(
    migrations: Path, tmp_path: Path
) -> None:
    db = str(tmp_path / "augur.db")
    _migrator(migrations, tmp_path).migrate(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE schema_migrations SET compat='contract' WHERE version=2")
    conn.commit()
    conn.close()
    (migrations / "0002_ohlcv.sql").unlink()
    m = Migrator(migrations, backup_dir=tmp_path / "b", app_version="0.3.1", expected_schema=8)
    with pytest.raises(UnknownAppliedMigrationError):
        m.migrate(db)


def test_cli_migrate_reports_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from augur.interfaces.cli.main import app

    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "augur.db")
    runner = CliRunner()
    assert runner.invoke(app, ["db", "migrate", "--db", db]).exit_code == 0
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO schema_migrations(version, name, checksum, compat, app_version) "
        "VALUES (999, '0999_x', 'x', 'contract', '9.9.9')"
    )
    conn.commit()
    conn.close()
    res = runner.invoke(app, ["db", "migrate", "--db", db])
    assert res.exit_code == 1
    assert "Обновите пакет augur" in res.output
