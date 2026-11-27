"""Репозиторий сигналов (ARCHITECTURE.md §3.5): `signals` (strategy/final) и `fused_signals`.

Синхронный минимум на `sqlite3` — как `augur.db.audit`. Полноценный `DbWriter`
(single writer, asyncio) — этап 0.6.0. Методы не делают COMMIT, кроме явно указанных:
транзакцией управляет вызывающий.
"""

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from augur.fusion.contract import FusedSignal, RiskFlag
from augur.ids import new_ulid

if TYPE_CHECKING:
    from augur.signals.engine import StrategySignal
    from augur.signals.ready import SignalReady

__all__ = ["SignalRepository", "SignalStatus"]

SignalStatus = Literal["NEW", "SENT_TO_RISK", "APPROVED", "REJECTED", "EXPIRED", "EXECUTED"]


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _flags_json(flags: Sequence[RiskFlag]) -> str:
    data = [f.model_dump(mode="json") for f in flags]
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class SignalRepository:
    """Запись сигналов в SQLite.

    Args:
        conn: соединение с применёнными миграциями (`PRAGMA foreign_keys=ON`).
        config_hash: `config_hash(settings)` — пишется в каждый сигнал.
        code_version: версия кода решения (`augur.__version__`).
    """

    def __init__(self, conn: sqlite3.Connection, *, config_hash: str, code_version: str) -> None:
        self._conn = conn
        self._config_hash = config_hash
        self._code_version = code_version

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def insert_strategy_signal(self, s: "StrategySignal", *, created_ts: int) -> bool:
        """`signals(kind='strategy')`. False — дубликат (тот же бар/стратегия/параметры или id)."""
        cur = self._conn.execute(
            "INSERT INTO signals(id, kind, strategy, params_hash, symbol, timeframe, action, "
            "strength, source, data_hash, config_hash, code_version, bar_ts, created_ts) "
            "VALUES (?, 'strategy', ?, ?, ?, ?, ?, ?, 'classic', ?, ?, ?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            (
                s.signal_id,
                s.strategy_version,
                s.params_hash,
                s.symbol,
                s.timeframe,
                s.action.value,
                s.strength,
                s.data_hash,
                self._config_hash,
                self._code_version,
                s.timestamp,
                created_ts,
            ),
        )
        return cur.rowcount == 1

    def save_fused(self, fused: FusedSignal, *, valid: bool, errors: Sequence[str]) -> str:
        """`fused_signals`; идемпотентно по `signal_id`/`idempotency_key` — возвращает id строки."""
        new_id = new_ulid()
        self._conn.execute(
            "INSERT INTO fused_signals(id, signal_id, idempotency_key, api_version, asset, "
            "timestamp, expires_at, action, strength, reason, explanation_ref, risk_flags_json, "
            "source_breakdown_json, fusion_config_version, valid, validation_errors_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (
                new_id,
                fused.signal_id,
                fused.idempotency_key,
                fused.api_version,
                fused.asset,
                fused.timestamp,
                fused.expires_at,
                fused.action.value,
                fused.strength,
                fused.reason,
                fused.explanation_ref,
                _flags_json(fused.risk_flags),
                json.dumps(fused.source_breakdown, sort_keys=True, separators=(",", ":")),
                fused.fusion_config_version,
                1 if valid else 0,
                json.dumps(list(errors), ensure_ascii=False),
            ),
        )
        row = self._conn.execute(
            "SELECT id FROM fused_signals WHERE signal_id = ? OR idempotency_key = ?",
            (fused.signal_id, fused.idempotency_key),
        ).fetchone()
        return str(row[0])

    def insert_final(self, sig: "SignalReady", *, timeframe: str | None = None) -> None:
        """`signals(kind='final')` — итоговый сигнал, на который ссылается `decision_audit`."""
        self._conn.execute(
            "INSERT INTO signals(id, kind, parent_ids_json, symbol, timeframe, action, strength, "
            "source, fused_signal_id, risk_flags_json, explanation_ref, stop_loss, take_profit, "
            "config_hash, code_version, created_ts, expires_ts) "
            "VALUES (?, 'final', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sig.signal_id,
                json.dumps(sig.strategy_signal_ids),
                sig.symbol,
                timeframe,
                sig.action.value,
                sig.strength,
                sig.source,
                sig.fused_signal_id,
                _flags_json(sig.risk_flags),
                sig.explanation_ref,
                None if sig.stop_loss is None else str(sig.stop_loss),
                None if sig.take_profit is None else str(sig.take_profit),
                self._config_hash,
                self._code_version,
                _ms(sig.created_at),
                _ms(sig.expires_at),
            ),
        )

    def set_status(self, signal_id: str, status: SignalStatus) -> None:
        cur = self._conn.execute("UPDATE signals SET status = ? WHERE id = ?", (status, signal_id))
        if cur.rowcount != 1:
            raise KeyError(f"signal not found: {signal_id}")

    def commit(self) -> None:
        self._conn.commit()
