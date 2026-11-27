# ruff: noqa: S608 — в SQL подставляются только константы колонок и плейсхолдеры "?"
"""Сервис чтения для GUI, REST и CLI (ARCHITECTURE.md §7.5, §9).

Только SELECT: ничего не пишет и не меняет состояние. REST открывает БД в режиме
`mode=ro`, так что запись невозможна и на уровне SQLite.
"""

import json
import sqlite3
import time
from typing import Any

from augur import __version__
from augur.config.settings import Settings
from augur.db.audit import compute_row_hash
from augur.interfaces.dto import (
    AuditChainReportDTO,
    AuditEventDTO,
    DecisionAuditDTO,
    DecisionChainDTO,
    FillDTO,
    FusedSignalDTO,
    NewsDTO,
    NewsReactionDTO,
    OrderDTO,
    PortfolioSnapshotDTO,
    PositionDTO,
    SignalDTO,
    StatusDTO,
)
from augur.versions import API_VERSION, DB_SCHEMA_VERSION

__all__ = ["ChainNotFoundError", "ReadService", "verify_audit_chain"]

ACTIVE_ORDER_STATES = ("PENDING", "SUBMITTED", "PARTIAL", "UNKNOWN")

_SIGNAL_COLS = (
    "id, kind, strategy, symbol, timeframe, action, strength, source, status, fused_signal_id, "
    "parent_ids_json, risk_flags_json, explanation_ref, stop_loss, take_profit, data_hash, "
    "config_hash, code_version, bar_ts, created_ts, expires_ts"
)
_ORDER_COLS = (
    "id, client_order_id, venue_order_id, signal_id, risk_verdict_id, venue, symbol, side, type, "
    "qty, limit_price, stop_price, filled_qty, avg_fill_price, fee, fee_ccy, state, mode, "
    "reject_reason, created_ts, updated_ts"
)
_POSITION_COLS = (
    "venue, symbol, mode, qty, avg_price, realized_pnl, unrealized_pnl, stop_loss, take_profit, "
    "opened_ts, updated_ts"
)
_AUDIT_COLS = "id, ts, actor, event, severity, ref_type, ref_id, payload_json"
_NEWS_COLS = (
    "id, source, url, title, summary, published_ts, fetched_ts, symbols_json, sentiment, "
    "sentiment_method, confidence, importance"
)
_REACTION_COLS = (
    "id, rule, action, symbol, strategy, flag_code, flag_severity, news_id, created_ts, expires_ts"
)


class ChainNotFoundError(LookupError):
    """Нет ни сигнала, ни ордера с таким id."""


def _json(raw: str | None, default: Any) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def _dict(cols: str, row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip([c.strip() for c in cols.split(",")], row, strict=True))


def _signal(row: tuple[Any, ...]) -> SignalDTO:
    d = _dict(_SIGNAL_COLS, row)
    d["parent_ids"] = _json(d.pop("parent_ids_json"), [])
    d["risk_flags"] = _json(d.pop("risk_flags_json"), [])
    return SignalDTO(**d)


def _audit(row: tuple[Any, ...]) -> AuditEventDTO:
    d = _dict(_AUDIT_COLS, row)
    d["payload"] = _json(d.pop("payload_json"), {})
    return AuditEventDTO(**d)


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


class ReadService:
    """Args:
    conn: соединение с БД (схема не ниже `DB_SCHEMA_VERSION`).
    settings: загруженные настройки; None — без полей конфига в статусе.
    clock: «сейчас», Unix ms UTC (срок активных реакций, `server_ts`).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: Settings | None = None,
        clock: Any = None,
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._clock = clock or (lambda: int(time.time() * 1000))

    # --- статус ---------------------------------------------------------------------

    def status(self) -> StatusDTO:
        c = self._conn
        now = int(self._clock())
        mode = c.execute(
            "SELECT mode, changed_ts, changed_by FROM mode_state WHERE id=1"
        ).fetchone()
        cb = c.execute("SELECT state, reason, tripped_ts FROM cb_state WHERE id=1").fetchone()
        open_orders = c.execute(
            f"SELECT COUNT(*) FROM orders WHERE state IN ({_placeholders(4)})", ACTIVE_ORDER_STATES
        ).fetchone()[0]
        awaiting = c.execute(
            "SELECT COUNT(*) FROM orders o WHERE o.state = 'PENDING' AND EXISTS ("
            " SELECT 1 FROM audit_log a WHERE a.ref_type='order' AND a.ref_id=o.id"
            " AND a.event='ORDER_APPROVAL_REQUIRED') AND NOT EXISTS ("
            " SELECT 1 FROM audit_log a WHERE a.ref_type='order' AND a.ref_id=o.id"
            " AND a.event='ORDER_APPROVED')"
        ).fetchone()[0]
        positions = c.execute(
            "SELECT COUNT(*) FROM positions WHERE CAST(qty AS REAL) != 0"
        ).fetchone()[0]
        reactions = c.execute(
            "SELECT COUNT(*) FROM news_reactions WHERE lifted_ts IS NULL AND expires_ts > ?",
            (now,),
        ).fetchone()[0]
        last_audit = c.execute("SELECT MAX(id) FROM audit_log").fetchone()[0]
        s = self._settings
        return StatusDTO(
            app_version=__version__,
            db_schema_version=DB_SCHEMA_VERSION,
            api_version=API_VERSION,
            configured_mode=s.trading_mode.value if s else None,
            max_allowed_mode=s.max_allowed_mode.value if s else None,
            mode=mode[0] if mode else None,
            mode_changed_ts=mode[1] if mode else None,
            mode_changed_by=mode[2] if mode else None,
            cb_state=cb[0] if cb else "ARMED",
            cb_reason=cb[1] if cb else None,
            cb_tripped_ts=cb[2] if cb else None,
            fusion_enabled=s.fusion.enabled if s else None,
            open_orders=open_orders,
            awaiting_approval=awaiting,
            open_positions=positions,
            active_news_reactions=reactions,
            last_audit_id=last_audit,
            server_ts=now,
        )

    # --- сигналы и цепочка решения ----------------------------------------------------

    def signals(
        self,
        *,
        symbol: str | None = None,
        since: int | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[SignalDTO]:
        where: list[str] = []
        args: list[Any] = []
        if symbol:
            where.append("symbol = ?")
            args.append(symbol)
        if since is not None:
            where.append("created_ts >= ?")
            args.append(since)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        sql = f"SELECT {_SIGNAL_COLS} FROM signals"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_ts DESC, id DESC LIMIT ?"
        args.append(limit)
        return [_signal(r) for r in self._conn.execute(sql, args)]

    def _signal_by_id(self, signal_id: str) -> SignalDTO | None:
        row = self._conn.execute(
            f"SELECT {_SIGNAL_COLS} FROM signals WHERE id = ?", (signal_id,)
        ).fetchone()
        return _signal(row) if row else None

    def decision_chain(self, ref_id: str) -> DecisionChainDTO:
        """Цепочка по id сигнала (strategy или final) либо ордера.

        Raises:
            ChainNotFoundError: такого сигнала/ордера нет.
        """
        sig = self._signal_by_id(ref_id)
        if sig is None:
            row = self._conn.execute(
                "SELECT signal_id FROM orders WHERE id = ?", (ref_id,)
            ).fetchone()
            if row is None:
                raise ChainNotFoundError(ref_id)
            sig = self._signal_by_id(str(row[0]))
            if sig is None:  # FK orders.signal_id → signals.id; защитно
                raise ChainNotFoundError(ref_id)
        if sig.kind == "strategy":
            # стратегический сигнал → итоговый, в parent_ids которого он входит
            row = self._conn.execute(
                f"SELECT {_SIGNAL_COLS} FROM signals WHERE kind='final' AND EXISTS ("
                " SELECT 1 FROM json_each(signals.parent_ids_json) WHERE value = ?)"
                " ORDER BY created_ts LIMIT 1",
                (sig.id,),
            ).fetchone()
            final = _signal(row) if row else None
            strategy_signals = [sig] if final is None else self._parents(final)
            if final is not None:
                sig = final
        else:
            strategy_signals = self._parents(sig)
        fused = self._fused(sig.fused_signal_id) if sig.fused_signal_id else None
        decisions = self._decisions(sig.id)
        orders = self.orders_for_signal(sig.id)
        positions = [
            p
            for p in self.positions()
            if p.symbol == sig.symbol and {p.mode} & {o.mode for o in orders}
        ]
        signal_ids = [sig.id] + [s.id for s in strategy_signals]
        order_ids = [o.id for o in orders]
        events = self._events_for(signal_ids, order_ids)
        return DecisionChainDTO(
            requested_id=ref_id,
            signal=sig,
            strategy_signals=strategy_signals,
            fused_signal=fused,
            decisions=decisions,
            orders=orders,
            positions=positions,
            events=events,
        )

    def _parents(self, final: SignalDTO) -> list[SignalDTO]:
        if not final.parent_ids:
            return []
        ids = [str(i) for i in final.parent_ids]
        rows = self._conn.execute(
            f"SELECT {_SIGNAL_COLS} FROM signals WHERE id IN ({_placeholders(len(ids))})"
            " ORDER BY created_ts, id",
            ids,
        )
        return [_signal(r) for r in rows]

    def _fused(self, fused_id: str) -> FusedSignalDTO | None:
        row = self._conn.execute(
            "SELECT id, signal_id, api_version, asset, timestamp, expires_at, action, strength, "
            "reason, risk_flags_json, source_breakdown_json, fusion_config_version, valid, "
            "validation_errors_json FROM fused_signals WHERE id = ?",
            (fused_id,),
        ).fetchone()
        if row is None:
            return None
        return FusedSignalDTO(
            id=row[0],
            signal_id=row[1],
            api_version=row[2],
            asset=row[3],
            timestamp=row[4],
            expires_at=row[5],
            action=row[6],
            strength=row[7],
            reason=row[8],
            risk_flags=_json(row[9], []),
            source_breakdown=_json(row[10], {}),
            fusion_config_version=row[11],
            valid=bool(row[12]),
            validation_errors=_json(row[13], []),
        )

    def _decisions(self, signal_id: str) -> list[DecisionAuditDTO]:
        cols = (
            "id, signal_id, fused_signal_id, order_id, app_version, strategy_version, "
            "ml_model_version, llm_prompt_version, ml_prediction_id, llm_status, risk_verdict, "
            "risk_reason, config_hash, mode, execution_result, explanation, created_at"
        )
        out = []
        for r in self._conn.execute(
            f"SELECT {cols} FROM decision_audit WHERE signal_id = ? ORDER BY created_at, id",
            (signal_id,),
        ):
            d = _dict(cols, r)
            d["execution_result"] = _json(d["execution_result"], None)
            out.append(DecisionAuditDTO(**d))
        return out

    def _events_for(self, signal_ids: list[str], order_ids: list[str]) -> list[AuditEventDTO]:
        clauses, args = [], []
        if signal_ids:
            clauses.append(f"(ref_type='signal' AND ref_id IN ({_placeholders(len(signal_ids))}))")
            args.extend(signal_ids)
        if order_ids:
            clauses.append(f"(ref_type='order' AND ref_id IN ({_placeholders(len(order_ids))}))")
            args.extend(order_ids)
        if not clauses:
            return []
        rows = self._conn.execute(
            f"SELECT {_AUDIT_COLS} FROM audit_log WHERE {' OR '.join(clauses)} ORDER BY id", args
        )
        return [_audit(r) for r in rows]

    # --- торговые данные --------------------------------------------------------------

    def _fills(self, order_ids: list[str]) -> dict[str, list[FillDTO]]:
        out: dict[str, list[FillDTO]] = {i: [] for i in order_ids}
        if not order_ids:
            return out
        for r in self._conn.execute(
            "SELECT id, order_id, venue_fill_id, qty, price, fee, ts FROM fills "
            f"WHERE order_id IN ({_placeholders(len(order_ids))}) ORDER BY ts, id",
            order_ids,
        ):
            out[r[1]].append(
                FillDTO(
                    id=r[0],
                    order_id=r[1],
                    venue_fill_id=r[2],
                    qty=r[3],
                    price=r[4],
                    fee=r[5],
                    ts=r[6],
                )
            )
        return out

    def _orders(self, sql_tail: str, args: list[Any]) -> list[OrderDTO]:
        rows = [
            _dict(_ORDER_COLS, r)
            for r in self._conn.execute(f"SELECT {_ORDER_COLS} FROM orders {sql_tail}", args)
        ]
        fills = self._fills([r["id"] for r in rows])
        return [OrderDTO(**r, fills=fills[r["id"]]) for r in rows]

    def orders(
        self, *, state: str | None = None, symbol: str | None = None, limit: int = 50
    ) -> list[OrderDTO]:
        where: list[str] = []
        args: list[Any] = []
        if state:
            where.append("state = ?")
            args.append(state.upper())
        if symbol:
            where.append("symbol = ?")
            args.append(symbol)
        tail = (" WHERE " + " AND ".join(where)) if where else ""
        return self._orders(tail + " ORDER BY created_ts DESC, id DESC LIMIT ?", [*args, limit])

    def orders_for_signal(self, signal_id: str) -> list[OrderDTO]:
        return self._orders("WHERE signal_id = ? ORDER BY created_ts, id", [signal_id])

    def positions(self, *, include_flat: bool = False) -> list[PositionDTO]:
        sql = f"SELECT {_POSITION_COLS} FROM positions"
        if not include_flat:
            sql += " WHERE CAST(qty AS REAL) != 0"
        sql += " ORDER BY mode, venue, symbol"
        return [PositionDTO(**_dict(_POSITION_COLS, r)) for r in self._conn.execute(sql)]

    def snapshots(
        self, *, mode: str | None = None, since: int | None = None, limit: int = 500
    ) -> list[PortfolioSnapshotDTO]:
        """Снимки портфеля по возрастанию времени (последние `limit`)."""
        cols = "ts, mode, equity, cash, day_pnl, peak_equity, drawdown_pct"
        where: list[str] = []
        args: list[Any] = []
        if mode:
            where.append("mode = ?")
            args.append(mode)
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        tail = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._conn.execute(
            f"SELECT {cols} FROM portfolio_snapshots{tail} ORDER BY ts DESC LIMIT ?",
            [*args, limit],
        ).fetchall()
        return [PortfolioSnapshotDTO(**_dict(cols, r)) for r in reversed(rows)]

    # --- новости ----------------------------------------------------------------------

    def news(self, *, symbol: str | None = None, limit: int = 50) -> list[NewsDTO]:
        sql = f"SELECT {_NEWS_COLS} FROM news"
        args: list[Any] = []
        if symbol:
            sql += " WHERE EXISTS (SELECT 1 FROM json_each(news.symbols_json) WHERE value = ?)"
            args.append(symbol)
        sql += " ORDER BY published_ts DESC, id DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self._conn.execute(sql, args):
            d = _dict(_NEWS_COLS, r)
            d["symbols"] = _json(d.pop("symbols_json"), [])
            out.append(NewsDTO(**d))
        return out

    def news_reactions(self) -> list[NewsReactionDTO]:
        """Активные реакции: не сняты и не истекли."""
        rows = self._conn.execute(
            f"SELECT {_REACTION_COLS} FROM news_reactions WHERE lifted_ts IS NULL "
            "AND expires_ts > ? ORDER BY created_ts DESC",
            (int(self._clock()),),
        )
        return [NewsReactionDTO(**_dict(_REACTION_COLS, r)) for r in rows]

    # --- журнал ---------------------------------------------------------------------

    def audit_events(self, *, after_id: int = 0, limit: int = 200) -> list[AuditEventDTO]:
        """События `audit_log` с `id > after_id` по возрастанию (хвост для `/ws/events`)."""
        rows = self._conn.execute(
            f"SELECT {_AUDIT_COLS} FROM audit_log WHERE id > ? ORDER BY id LIMIT ?",
            (after_id, limit),
        )
        return [_audit(r) for r in rows]

    def recent_audit(self, *, limit: int = 50) -> list[AuditEventDTO]:
        rows = self._conn.execute(
            f"SELECT {_AUDIT_COLS} FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_audit(r) for r in rows]


def verify_audit_chain(conn: sqlite3.Connection) -> AuditChainReportDTO:
    """Проверяет hash-chain `audit_log` (ARCH §3.2, §7.5): `prev_hash` и `row_hash` каждой строки.

    Формула — `augur.db.audit.compute_row_hash` (та же, что при записи).
    """
    prev: str | None = None
    n = 0
    for row in conn.execute(
        "SELECT id, ts, actor, event, severity, ref_type, ref_id, payload_json, prev_hash, "
        "row_hash FROM audit_log ORDER BY id"
    ):
        n += 1
        rid, ts, actor, event, severity, ref_type, ref_id, payload_json, prev_hash, row_hash = row
        if prev_hash != prev:
            return AuditChainReportDTO(
                ok=False, rows_checked=n, first_bad_id=rid, problem="prev_hash mismatch"
            )
        expected = compute_row_hash(
            prev,
            {
                "ts": ts,
                "actor": actor,
                "event": event,
                "severity": severity,
                "ref_type": ref_type,
                "ref_id": ref_id,
                "payload_json": payload_json,
            },
        )
        if expected != row_hash:
            return AuditChainReportDTO(
                ok=False, rows_checked=n, first_bad_id=rid, problem="row_hash mismatch"
            )
        prev = row_hash
    return AuditChainReportDTO(ok=True, rows_checked=n)
