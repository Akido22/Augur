"""Мигратор SQLite: forward-only, по одной транзакции на миграцию (VERSIONING.md §2.2, §8).

- Миграции — `src/augur/db/migrations/NNNN_<name>.sql`, применяются по возрастанию NNNN.
- Каждая миграция объявляет совместимость строкой `-- compat: expand|contract`.
- Применённая миграция фиксируется в `schema_migrations` в той же транзакции, что и её DDL:
  при ошибке откатывается только она, предыдущие остаются, мигратор останавливается.
- checksum — sha256 текста миграции с нормализацией концов строк (CRLF → LF), чтобы
  `core.autocrlf` на Windows не давал ложных расхождений. Изменённая применённая
  миграция → `ChecksumMismatchError`.
- Перед применением ожидающих миграций снимается бэкап (`VACUUM INTO`, VERSIONING §8.2).
- Down-миграций нет (VERSIONING §8.1).
- Схема сверяется с `augur.versions.DB_SCHEMA_VERSION` (VERSIONING §2.2):
  файл миграции новее константы — отказ до изменения БД; после применения схема ниже
  ожидаемой — отказ; БД новее кода допускается, только если все лишние миграции `expand`.
"""

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from augur import __version__
from augur.versions import DB_SCHEMA_VERSION

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_COMPAT = re.compile(r"^--\s*compat:\s*(expand|contract)\s*$", re.MULTILINE)

__all__ = [
    "MIGRATIONS_DIR",
    "ChecksumMismatchError",
    "MigrationError",
    "MigrationFailedError",
    "MigrationFile",
    "MigrationResult",
    "MigrationStatus",
    "Migrator",
    "SchemaVersionMismatchError",
    "UnknownAppliedMigrationError",
    "file_checksum",
]


class MigrationError(Exception):
    """Базовая ошибка мигратора."""


class ChecksumMismatchError(MigrationError):
    """Файл применённой миграции изменён после применения."""


class MigrationFailedError(MigrationError):
    """Миграция упала и откачена; следующие не применялись."""


class UnknownAppliedMigrationError(MigrationError):
    """В БД есть contract-миграция, которой нет в коде (БД новее кода)."""


class SchemaVersionMismatchError(MigrationError):
    """Версия схемы БД или файлов миграций не совпадает с `DB_SCHEMA_VERSION` кода."""


@dataclass(frozen=True)
class MigrationFile:
    """Миграция на диске."""

    version: int
    name: str  # 0001_core
    path: Path
    checksum: str
    compat: Literal["expand", "contract"]
    sql: str


@dataclass(frozen=True)
class MigrationStatus:
    """Состояние одной миграции относительно БД."""

    version: int
    name: str
    state: Literal["applied", "pending", "missing_file"]
    checksum: str
    applied_at: int | None  # Unix ms UTC
    compat: str


@dataclass(frozen=True)
class MigrationResult:
    """Итог `migrate()`."""

    applied: list[str]
    backup_path: str | None
    schema_version: int  # MAX(version) после миграции, 0 — пустая БД


def file_checksum(text: str) -> str:
    """sha256 текста миграции после нормализации концов строк."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)  # транзакции — явные BEGIN/COMMIT
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class Migrator:
    """Применяет миграции из каталога к файлу SQLite."""

    def __init__(
        self,
        migrations_dir: Path = MIGRATIONS_DIR,
        *,
        backup_dir: Path = Path("backups"),
        app_version: str = __version__,
        expected_schema: int = DB_SCHEMA_VERSION,
    ) -> None:
        """
        Args:
            migrations_dir: каталог с `NNNN_*.sql`.
            backup_dir: куда класть бэкапы (по умолчанию `backups/` от текущего каталога).
            app_version: версия Augur, пишется в `schema_migrations.app_version`.
            expected_schema: схема, которую ожидает код (`augur.versions.DB_SCHEMA_VERSION`).
        """
        self._dir = migrations_dir
        self._backup_dir = backup_dir
        self._app_version = app_version
        self._expected = expected_schema

    # --- чтение -----------------------------------------------------------------

    def discover(self) -> list[MigrationFile]:
        """Файлы миграций по возрастанию версии.

        Raises:
            MigrationError: неверное имя файла, дубль версии или нет строки `-- compat:`.
        """
        files: list[MigrationFile] = []
        seen: dict[int, str] = {}
        for path in sorted(self._dir.glob("*.sql")):
            m = _FILENAME.match(path.name)
            if not m:
                raise MigrationError(
                    f"bad migration filename: {path.name} (ожидается NNNN_name.sql)"
                )
            version = int(m.group(1))
            if version in seen:
                raise MigrationError(
                    f"duplicate migration version {version}: {seen[version]}, {path.name}"
                )
            seen[version] = path.name
            text = path.read_text(encoding="utf-8")
            compat = _COMPAT.search(text)
            if compat is None:
                raise MigrationError(f"{path.name}: нет строки '-- compat: expand|contract'")
            files.append(
                MigrationFile(
                    version=version,
                    name=path.stem,
                    path=path,
                    checksum=file_checksum(text),
                    compat=compat.group(1),  # type: ignore[arg-type]
                    sql=text,
                )
            )
        return files

    @staticmethod
    def _applied(conn: sqlite3.Connection) -> dict[int, tuple[str, str, int, str]]:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if not exists:
            return {}
        rows = conn.execute(
            "SELECT version, name, checksum, applied_at, compat FROM schema_migrations"
        ).fetchall()
        return {int(v): (n, c, int(a), cp) for v, n, c, a, cp in rows}

    def status(self, db_path: str) -> list[MigrationStatus]:
        """Применённые, ожидающие и «потерянные» (есть в БД, нет файла) миграции."""
        files = {f.version: f for f in self.discover()}
        conn = _connect(db_path)
        try:
            applied = self._applied(conn)
        finally:
            conn.close()
        out: list[MigrationStatus] = []
        for version in sorted(files.keys() | applied.keys()):
            f = files.get(version)
            if version in applied:
                name, checksum, applied_at, compat = applied[version]
                state: Literal["applied", "pending", "missing_file"] = (
                    "applied" if f is not None else "missing_file"
                )
                out.append(MigrationStatus(version, name, state, checksum, applied_at, compat))
            elif f is not None:
                out.append(MigrationStatus(version, f.name, "pending", f.checksum, None, f.compat))
        return out

    # --- изменение --------------------------------------------------------------

    def _verify(
        self, files: list[MigrationFile], applied: dict[int, tuple[str, str, int, str]]
    ) -> None:
        by_version = {f.version: f for f in files}
        for version, (name, checksum, _, compat) in sorted(applied.items()):
            f = by_version.get(version)
            if f is None:
                if compat != "expand":
                    raise UnknownAppliedMigrationError(
                        f"БД содержит contract-миграцию {name}, которой нет в коде: обновите код"
                    )
                continue  # БД новее кода, но только expand — совместимо (VERSIONING §2.2)
            if f.checksum != checksum:
                raise ChecksumMismatchError(
                    f"{f.path.name}: checksum {f.checksum[:12]}… ≠ применённой {checksum[:12]}…; "
                    "применённые миграции не редактируются — создайте новую"
                )

    def _check_files(self, files: list[MigrationFile]) -> None:
        """Файл миграции новее константы — код и миграции рассинхронизированы."""
        newer = [f.name for f in files if f.version > self._expected]
        if newer:
            raise SchemaVersionMismatchError(
                f"migration {', '.join(newer)} on disk > code expects {self._expected}. "
                "Обновите DB_SCHEMA_VERSION в augur/versions.py"
            )

    def _check_db_not_newer(self, applied: dict[int, tuple[str, str, int, str]]) -> None:
        """БД новее кода — допустимо, только если все лишние миграции expand (VERSIONING §2.2)."""
        extra = {v: row for v, row in applied.items() if v > self._expected}
        if any(compat != "expand" for _, _, _, compat in extra.values()):
            raise SchemaVersionMismatchError(
                f"DB schema {max(extra)} > code expects {self._expected}. Обновите пакет augur"
            )

    def _check_db_not_older(self, schema: int) -> None:
        if schema < self._expected:
            raise SchemaVersionMismatchError(
                f"DB schema {schema} < expected {self._expected}. "
                "Примените миграции: augur db migrate"
            )

    def backup_before_migrate(self, db_path: str) -> str:
        """Консистентная копия БД через `VACUUM INTO`.

        Имя: `augur_pre_v<app>_schema<N>_<UTC timestamp>.db` (VERSIONING §8.2).

        Returns:
            Путь к файлу бэкапа.
        """
        conn = _connect(db_path)
        try:
            applied = self._applied(conn)
            schema = max(applied, default=0)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            self._backup_dir.mkdir(parents=True, exist_ok=True)
            target = self._backup_dir / f"augur_pre_v{self._app_version}_schema{schema}_{stamp}.db"
            conn.execute("VACUUM INTO ?", (str(target),))
        finally:
            conn.close()
        return str(target)

    def migrate(self, db_path: str) -> MigrationResult:
        """Применяет все ожидающие миграции.

        Raises:
            ChecksumMismatchError: изменён файл применённой миграции (ничего не применяется).
            UnknownAppliedMigrationError: БД новее кода и содержит contract-миграцию.
            MigrationFailedError: миграция упала; она откачена, предыдущие сохранены.
            SchemaVersionMismatchError: файл миграции новее `DB_SCHEMA_VERSION`; БД новее кода
                с не-expand миграцией; после применения схема ниже `DB_SCHEMA_VERSION`.
            MigrationError: версия приложения неизвестна (`+unknown`) — запуск без установки.
        """
        if self._app_version.endswith("+unknown"):
            raise MigrationError("версия приложения неизвестна: выполните pip install -e .")
        files = self.discover()
        self._check_files(files)
        conn = _connect(db_path)
        try:
            applied = self._applied(conn)
        finally:
            conn.close()
        self._check_db_not_newer(applied)
        self._verify(files, applied)
        pending = [f for f in files if f.version not in applied]
        if not pending:
            schema = max(applied, default=0)
            self._check_db_not_older(schema)
            return MigrationResult([], None, schema)

        backup = self.backup_before_migrate(db_path)
        done: list[str] = []
        conn = _connect(db_path)
        try:
            for f in pending:
                try:
                    # DDL и запись в schema_migrations — одна транзакция: BEGIN открывается
                    # в скрипте, INSERT идёт параметризованно, затем COMMIT.
                    conn.executescript(f"BEGIN;\n{f.sql}\n;")
                    conn.execute(
                        "INSERT INTO schema_migrations(version, name, checksum, compat, "
                        "app_version) VALUES (?, ?, ?, ?, ?)",
                        (f.version, f.name, f.checksum, f.compat, self._app_version),
                    )
                    conn.execute("COMMIT")
                except sqlite3.Error as e:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise MigrationFailedError(
                        f"{f.name} откачена: {e}. Применены до неё: {done or 'нет'}; "
                        f"бэкап: {backup}"
                    ) from e
                done.append(f.name)
            schema = max(self._applied(conn), default=0)
        finally:
            conn.close()
        self._check_db_not_older(schema)
        return MigrationResult(done, backup, schema)
