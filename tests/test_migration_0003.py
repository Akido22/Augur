"""Миграция 0003_fusion_contract: fused_signals пересоздана, explanation_ref NOT NULL."""

import sqlite3
from pathlib import Path

import pytest

from augur.db.migrator import Migrator


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = str(tmp_path / "augur.db")
    result = Migrator(backup_dir=tmp_path / "b", app_version="0.4.0").migrate(db)
    assert result.applied[-1] == "0003_fusion_contract"
    c = sqlite3.connect(db)
    c.execute("PRAGMA foreign_keys=ON")
    return c


COLS = (
    "id, signal_id, idempotency_key, api_version, asset, timestamp, expires_at, action, "
    "strength, reason, explanation_ref, source_breakdown_json, fusion_config_version, valid"
)


def _insert(conn: sqlite3.Connection, n: int = 1, **over: object) -> None:
    row: dict[str, object] = {
        "id": f"01FS{n:022d}",
        "signal_id": f"uuid-{n}",
        "idempotency_key": f"key-{n}",
        "api_version": "1.0",
        "asset": "BTC/USDT",
        "timestamp": 1_000,
        "expires_at": 2_000,
        "action": "HOLD",
        "strength": 0.0,
        "reason": "fusion_stub",
        "explanation_ref": "fusion_stub",
        "source_breakdown_json": '{"ml": 0.0, "tech": 1.0, "llm": 0.0, "rules_fired": []}',
        "fusion_config_version": "stub-0.4.0",
        "valid": 1,
    }
    row.update(over)
    cols = ", ".join(row)
    conn.execute(
        f"INSERT INTO fused_signals ({cols}) VALUES ({', '.join('?' * len(row))})",  # noqa: S608
        list(row.values()),
    )


def test_applies_on_clean_db(conn: sqlite3.Connection) -> None:
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
    assert versions == [1, 2, 3]
    compat = conn.execute("SELECT compat FROM schema_migrations WHERE version=3").fetchone()[0]
    assert compat == "contract"
    _insert(conn)
    assert conn.execute("SELECT COUNT(*) FROM fused_signals").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_table_is_strict_and_indexes_exist(conn: sqlite3.Connection) -> None:
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='fused_signals'").fetchone()[0]
    assert sql.rstrip().endswith("STRICT")
    strict = conn.execute(
        "SELECT strict FROM pragma_table_list WHERE name='fused_signals'"
    ).fetchone()
    if strict is not None:  # pragma_table_list — SQLite ≥ 3.37
        assert strict[0] == 1
    indexes = {r[1] for r in conn.execute("PRAGMA index_list(fused_signals)")}
    assert {"ix_fused_asset_ts", "ix_fused_created"} <= indexes
    assert "fused_signals_new" not in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    with pytest.raises(sqlite3.IntegrityError):  # STRICT: TEXT в INTEGER-колонке
        _insert(conn, timestamp="not-an-int")


def test_explanation_ref_not_null(conn: sqlite3.Connection) -> None:
    notnull = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(fused_signals)")}
    assert notnull["explanation_ref"] == 1
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        conn.execute(
            "INSERT INTO fused_signals (id, signal_id, idempotency_key, api_version, asset, "
            "timestamp, expires_at, action, strength, reason, source_breakdown_json, "
            "fusion_config_version, valid) VALUES ('01X', 'u', 'k', '1.0', 'A', 1, 2, 'HOLD', "
            "0.0, 'r', '{}', 'v', 1)"
        )
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        _insert(conn, explanation_ref=None)


@pytest.mark.parametrize("action", ["hold", "enter_long", "BUY_MORE"])
def test_action_check(conn: sqlite3.Connection, action: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert(conn, action=action)


def test_all_six_actions_accepted(conn: sqlite3.Connection) -> None:
    for i, action in enumerate(["BUY", "SELL", "CLOSE", "REDUCE", "HOLD", "NO_SIGNAL"], start=1):
        _insert(conn, n=i, action=action)


def test_unique_signal_id_and_idempotency_key(conn: sqlite3.Connection) -> None:
    _insert(conn, n=1)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE.*signal_id"):
        _insert(conn, n=2, signal_id="uuid-1")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE.*idempotency_key"):
        _insert(conn, n=3, idempotency_key="key-1")


def test_valid_and_errors_defaults(conn: sqlite3.Connection) -> None:
    _insert(conn)
    row = conn.execute(
        "SELECT validation_errors_json, risk_flags_json FROM fused_signals"
    ).fetchone()
    assert row == ("[]", "[]")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert(conn, n=2, valid=2)


def test_signals_fk_still_points_to_fused_signals(conn: sqlite3.Connection) -> None:
    for table in ("signals", "decision_audit"):
        targets = {r[2] for r in conn.execute(f"PRAGMA foreign_key_list({table})")}
        assert "fused_signals" in targets
