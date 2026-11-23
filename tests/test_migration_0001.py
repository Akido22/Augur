"""Миграция 0001_core: применяется к пустой БД, все таблицы STRICT, время — INTEGER ms."""

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

MIGRATION = (
    Path(__file__).resolve().parents[1] / "src" / "augur" / "db" / "migrations" / "0001_core.sql"
)

EXPECTED_TABLES = {
    "schema_migrations",
    "artifacts",
    "signals",
    "fused_signals",
    "orders",
    "decision_audit",
    "audit_log",
    "mode_state",
    "cb_state",
}


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(MIGRATION.read_text(encoding="utf-8"))
    yield c
    c.close()


def _now_ms() -> int:
    return int(time.time() * 1000)


def test_all_tables_strict(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT name, strict FROM pragma_table_list WHERE schema='main' AND type='table' "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    assert {name for name, _ in rows} == EXPECTED_TABLES
    assert all(strict == 1 for _, strict in rows), rows


def test_no_ml_llm_tables(conn: sqlite3.Connection) -> None:
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not names & {"ml_models", "ml_predictions", "llm_calls"}


def _insert_signal(conn: sqlite3.Connection, signal_id: str = "01SIG") -> None:
    conn.execute(
        "INSERT INTO signals(id, kind, symbol, action, strength, source, config_hash, "
        "code_version, created_ts) VALUES (?, 'final', 'BTC/USDT', 'NO_SIGNAL', 0, "
        "'classic', 'h', 'sha', ?)",
        (signal_id, _now_ms()),
    )


def _insert_audit(conn: sqlite3.Connection, app_version: str = "0.1.0") -> None:
    conn.execute(
        "INSERT INTO decision_audit(id, signal_id, app_version, strategy_version, "
        "ml_model_version, llm_prompt_version, risk_verdict, config_hash, mode) "
        "VALUES ('01AUD', '01SIG', ?, 'sma_cross@1.0.0', 'n/a', 'n/a', 'REJECTED', 'h', 'PAPER')",
        (app_version,),
    )


def test_decision_audit_defaults_and_null_fused_signal(conn: sqlite3.Connection) -> None:
    _insert_signal(conn)
    _insert_audit(conn)  # fused_signal_id и order_id не заданы → NULL
    created_at, typ, fused = conn.execute(
        "SELECT created_at, typeof(created_at), fused_signal_id FROM decision_audit"
    ).fetchone()
    assert typ == "integer"
    assert abs(_now_ms() - created_at) < 60_000
    assert fused is None


def test_decision_audit_rejects_unknown_app_version(conn: sqlite3.Connection) -> None:
    _insert_signal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_audit(conn, app_version="0.0.0+unknown")


def test_decision_audit_append_only(conn: sqlite3.Connection) -> None:
    _insert_signal(conn)
    _insert_audit(conn)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE decision_audit SET risk_verdict='APPROVED'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM decision_audit")
    conn.execute("UPDATE decision_audit SET execution_result='{}'")  # write-once: NULL → значение
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE decision_audit SET execution_result='{\"x\":1}'")


def test_service_tables_time_is_integer_ms(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO schema_migrations(version, name, checksum, compat, app_version) "
        "VALUES (1, '0001_core', 'abc', 'expand', '0.1.0')"
    )
    conn.execute(
        "INSERT INTO artifacts(kind, version, sha256, path, metadata) "
        "VALUES ('strategy', 'sma_cross@1.0.0', 'abc', "
        "'artifacts/strategies/sma_cross@1.0.0.json', '{}')"
    )
    for table, col in (("schema_migrations", "applied_at"), ("artifacts", "created_at")):
        typ = conn.execute(f"SELECT typeof({col}) FROM {table}").fetchone()[0]  # noqa: S608
        assert typ == "integer"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO schema_migrations(version, name, checksum, compat, app_version, "
            "applied_at) VALUES (2, 'x', 'y', 'expand', '0.1.0', '2026-11-21T00:00:00Z')"
        )


def test_fused_signal_idempotency_key_unique(conn: sqlite3.Connection) -> None:
    sql = (
        "INSERT INTO fused_signals(id, signal_id, idempotency_key, api_version, asset, "
        "timestamp, expires_at, action, strength, reason, source_breakdown_json, "
        "fusion_config_version, valid) "
        "VALUES (?, ?, 'k1', '1.0', 'BTCUSDT@binance', 1, 2, 'HOLD', 0, 'ok', '{}', 'f-1', 1)"
    )
    conn.execute(sql, ("01F1", "uuid-1"))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute(sql, ("01F2", "uuid-2"))
