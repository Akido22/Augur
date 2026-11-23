"""Migrator: применение, идемпотентность, status, checksum, откат упавшей миграции, бэкап, CLI."""

import shutil
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from augur.db.migrator import (
    MIGRATIONS_DIR,
    ChecksumMismatchError,
    MigrationError,
    MigrationFailedError,
    Migrator,
    SchemaVersionMismatchError,
    file_checksum,
)
from augur.interfaces.cli.main import app

EXPECTED = ["0001_core", "0002_ohlcv"]


@pytest.fixture
def migrations(tmp_path: Path) -> Path:
    """Копия реальных миграций — тесты могут её портить, не трогая репозиторий."""
    target = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, target)
    return target


def _migrator(migrations: Path, tmp_path: Path, expected: int = 2) -> Migrator:
    """`expected` — DB_SCHEMA_VERSION «кода»; тесты с 0003+ на диске поднимают его."""
    return Migrator(
        migrations, backup_dir=tmp_path / "backups", app_version="0.1.0", expected_schema=expected
    )


def _rows(db: Path) -> list[tuple[int, str, str, str, str, int]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT version, name, checksum, compat, app_version, applied_at "
            "FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        conn.close()


def test_fresh_db_applies_all(migrations: Path, tmp_path: Path) -> None:
    db = tmp_path / "data" / "augur.db"
    result = _migrator(migrations, tmp_path).migrate(str(db))
    assert result.applied == EXPECTED
    assert result.schema_version == 2
    rows = _rows(db)
    assert [(v, n, cp, av) for v, n, _, cp, av, _ in rows] == [
        (1, "0001_core", "expand", "0.1.0"),
        (2, "0002_ohlcv", "expand", "0.1.0"),
    ]
    assert all(isinstance(r[5], int) and r[5] > 1_700_000_000_000 for r in rows)  # ms
    assert rows[0][2] == file_checksum((migrations / "0001_core.sql").read_text("utf-8"))


def test_second_migrate_is_noop(migrations: Path, tmp_path: Path) -> None:
    db = str(tmp_path / "augur.db")
    m = _migrator(migrations, tmp_path)
    m.migrate(db)
    again = m.migrate(db)
    assert again.applied == []
    assert again.backup_path is None
    assert again.schema_version == 2
    assert len(_rows(Path(db))) == 2


def test_status_before_and_after(migrations: Path, tmp_path: Path) -> None:
    db = str(tmp_path / "augur.db")
    m = _migrator(migrations, tmp_path)
    assert [(s.name, s.state) for s in m.status(db)] == [(n, "pending") for n in EXPECTED]
    m.migrate(db)
    st = m.status(db)
    assert [(s.name, s.state) for s in st] == [(n, "applied") for n in EXPECTED]
    assert all(s.applied_at is not None for s in st)


def test_checksum_mismatch_blocks_migrate(migrations: Path, tmp_path: Path) -> None:
    db = str(tmp_path / "augur.db")
    _migrator(migrations, tmp_path).migrate(db)
    m = _migrator(migrations, tmp_path, expected=3)
    path = migrations / "0002_ohlcv.sql"
    path.write_text(path.read_text("utf-8") + "\n-- правка истории\n", encoding="utf-8")
    (migrations / "0003_new.sql").write_text(
        "-- compat: expand\nCREATE TABLE t3 (x INTEGER) STRICT;\n", encoding="utf-8"
    )
    with pytest.raises(ChecksumMismatchError, match="0002_ohlcv"):
        m.migrate(db)
    assert len(_rows(Path(db))) == 2  # 0003 не применена


def test_crlf_does_not_change_checksum() -> None:
    assert file_checksum("a\nb\n") == file_checksum("a\r\nb\r\n")


def test_failed_migration_rolls_back_only_itself(migrations: Path, tmp_path: Path) -> None:
    db = str(tmp_path / "augur.db")
    (migrations / "0003_broken.sql").write_text(
        "-- compat: expand\nCREATE TABLE half_done (x INTEGER) STRICT;\nTHIS IS NOT SQL;\n",
        encoding="utf-8",
    )
    (migrations / "0004_after.sql").write_text(
        "-- compat: expand\nCREATE TABLE after_broken (x INTEGER) STRICT;\n", encoding="utf-8"
    )
    with pytest.raises(MigrationFailedError, match="0003_broken"):
        _migrator(migrations, tmp_path, expected=4).migrate(db)
    assert [r[1] for r in _rows(Path(db))] == EXPECTED
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "ohlcv" in tables
    assert "half_done" not in tables and "after_broken" not in tables


def test_backup_created_before_migration(migrations: Path, tmp_path: Path) -> None:
    db = str(tmp_path / "augur.db")
    _migrator(migrations, tmp_path).migrate(db)
    (migrations / "0003_extra.sql").write_text(
        "-- compat: expand\nCREATE TABLE extra (x INTEGER) STRICT;\n", encoding="utf-8"
    )
    result = _migrator(migrations, tmp_path, expected=3).migrate(db)
    assert result.applied == ["0003_extra"]
    backups = sorted((tmp_path / "backups").glob("augur_pre_v0.1.0_schema*.db"))
    assert len(backups) == 2  # перед первой миграцией и перед 0003
    assert result.backup_path is not None and "schema2" in result.backup_path
    conn = sqlite3.connect(result.backup_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "ohlcv" in tables and "extra" not in tables  # снимок до 0003


def test_db_newer_than_code(migrations: Path, tmp_path: Path) -> None:
    db = str(tmp_path / "augur.db")
    extra = migrations / "0003_extra.sql"
    extra.write_text("-- compat: expand\nCREATE TABLE e (x INTEGER) STRICT;\n", encoding="utf-8")
    _migrator(migrations, tmp_path, expected=3).migrate(db)  # «новый» код применил 0003
    extra.unlink()
    m = _migrator(migrations, tmp_path)  # откат кода: ожидает схему 2
    assert m.migrate(db).applied == []  # expand-миграция из будущего — совместимо
    assert m.status(db)[-1].state == "missing_file"

    conn = sqlite3.connect(db)
    conn.execute("UPDATE schema_migrations SET compat='contract' WHERE version=3")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaVersionMismatchError, match="DB schema 3 > code expects 2"):
        m.migrate(db)


def test_bad_files_rejected(migrations: Path, tmp_path: Path) -> None:
    (migrations / "0003_nocompat.sql").write_text("CREATE TABLE x (a INTEGER);\n", "utf-8")
    with pytest.raises(MigrationError, match="compat"):
        _migrator(migrations, tmp_path).discover()


def test_unknown_app_version_refused(migrations: Path, tmp_path: Path) -> None:
    m = Migrator(migrations, backup_dir=tmp_path / "b", app_version="0.0.0+unknown")
    with pytest.raises(MigrationError, match="pip install"):
        m.migrate(str(tmp_path / "augur.db"))


def test_cli_db_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # backups/ создаётся относительно текущего каталога
    db = str(tmp_path / "data" / "augur.db")
    runner = CliRunner()

    res = runner.invoke(app, ["db", "migrate", "--db", db])
    assert res.exit_code == 0, res.output
    assert "applied: 0001_core" in res.output and "applied: 0002_ohlcv" in res.output

    res = runner.invoke(app, ["db", "migrate", "--db", db])
    assert res.exit_code == 0 and "up to date: schema 2" in res.output

    res = runner.invoke(app, ["db", "status", "--db", db])
    assert res.exit_code == 0
    assert sum(" applied " in line for line in res.output.splitlines()) == 2

    res = runner.invoke(app, ["db", "backup", "--db", db])
    assert res.exit_code == 0 and "backup:" in res.output
    assert len(list((tmp_path / "backups").glob("*.db"))) == 2

    res = runner.invoke(app, ["db", "backup", "--db", str(tmp_path / "missing.db")])
    assert res.exit_code == 1
