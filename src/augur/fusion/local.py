"""`LocalFusionClient` — in-process реализация `FusionClient` (0.10.0; ARCH §5.4, FUSION SPEC §6.2).

На один закрытый бар: бары из `ohlcv` → ML-инференс (запись в `ml_predictions`) → последний
валидный LLM-контекст из `llm_contexts` (LLM не вызывается на критическом пути) → tech из
`FusionRequest.context` → `fuse()` → `verify()`. Позиция и состояние CB читаются из БД
только на чтение: Fusion не импортирует execution/risk. Исключение → Signal Engine уходит
в ML-disabled (`FUSION_FALLBACK`), ничего не исполняется.
"""

import sqlite3
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from augur.config.settings import FusionSettings
from augur.data.models import timeframe_ms
from augur.fusion.client import DECISION_AUDIT_REF_PREFIX, STUB_EXPLANATION_REF, FusionRequest
from augur.fusion.contract import FusedSignal
from augur.fusion.engine import TechSignal, fuse, verify
from augur.fusion.llm.service import LLMContextService
from augur.fusion.ml.data import load_bar_arrays
from augur.fusion.ml.inference import MLService

__all__ = ["LocalFusionClient", "read_breaker_open", "read_position_side", "tech_from_request"]

_TECH_SIGN = {"BUY": 1.0, "SELL": -1.0, "HOLD": 0.0}


def tech_from_request(request: FusionRequest) -> TechSignal | None:
    """Сигнал стратегии → `score ∈ [-1, 1]`: BUY +s, SELL −s, HOLD 0; иначе — нет голоса."""
    action = request.context.get("action")
    sign = _TECH_SIGN.get(str(action)) if action is not None else None
    if sign is None:
        return None
    strength = float(request.context.get("strength") or 0.0)
    return TechSignal(
        score=sign * max(0.0, min(1.0, strength)),
        timestamp=request.timestamp,
        detail={
            "action": action,
            "strength": strength,
            "strategy_version": request.context.get("strategy_version"),
            "strategy_signal_id": request.context.get("strategy_signal_id"),
        },
    )


def read_position_side(conn: sqlite3.Connection, symbol: str) -> int:
    """Знак суммарной позиции по символу в текущем режиме (`mode_state`, иначе PAPER)."""
    row = conn.execute("SELECT mode FROM mode_state WHERE id=1").fetchone()
    mode = str(row[0]) if row else "PAPER"
    total = Decimal(0)
    for (qty,) in conn.execute(
        "SELECT qty FROM positions WHERE symbol=? AND mode=?", (symbol, mode)
    ).fetchall():
        try:
            total += Decimal(str(qty))
        except InvalidOperation:
            continue
    return (total > 0) - (total < 0)


def read_breaker_open(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT state FROM cb_state WHERE id=1").fetchone()
    return bool(row) and str(row[0]) in ("TRIPPED", "KILLED")


class LocalFusionClient:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: FusionSettings,
        *,
        ml: MLService,
        llm: LLMContextService | None,
        clock: Callable[[], int],
        position_side: Callable[[str], int] | None = None,
        breaker_open: Callable[[], bool] | None = None,
    ) -> None:
        self._conn = conn
        self._cfg = settings
        self._ml = ml
        self._llm = llm
        self._clock = clock
        self._position_side = position_side or (lambda s: read_position_side(conn, s))
        self._breaker_open = breaker_open or (lambda: read_breaker_open(conn))

    async def get_fused_signal(self, request: FusionRequest) -> FusedSignal | None:
        now = self._clock()
        bar_ms = timeframe_ms(request.timeframe)
        ml_cfg = self._cfg.ml
        bars = load_bar_arrays(
            self._conn,
            venue=ml_cfg.venue,
            symbol=request.asset,
            timeframe=request.timeframe,
            until_close=request.timestamp,
            limit=ml_cfg.features.window,
        )
        pred = (
            self._ml.predict(request.asset, bars, request.timestamp) if bars is not None else None
        )
        llm = (
            self._llm.latest(request.asset, now_ms=now, max_age_s=self._cfg.config.llm_max_age_s)
            if self._llm is not None and self._cfg.llm.mode == "enabled"
            else None
        )
        decision_id = request.context.get("decision_audit_id")
        explanation_ref = (
            f"{DECISION_AUDIT_REF_PREFIX}{decision_id}"
            if isinstance(decision_id, str) and decision_id
            else STUB_EXPLANATION_REF
        )
        sig = fuse(
            pred,
            llm,
            tech_from_request(request),
            self._cfg.config,
            position_side=self._position_side(request.asset),
            breaker_open=self._breaker_open(),
            now_ms=now,
            bar_ms=bar_ms,
            asset=request.asset,
            timestamp=request.timestamp,
            idempotency_key=request.idempotency_key,
            explanation_ref=explanation_ref,
        )
        return verify(sig, pred)

    def describe(self) -> dict[str, Any]:
        return {"fusion_config_version": self._cfg.config.config_version, "llm": self._cfg.llm.mode}
