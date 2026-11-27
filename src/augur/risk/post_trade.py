"""Post-trade контроль (ARCHITECTURE.md §2.8, §7.4): метрики риска и триггеры circuit breaker.

- `evaluate_triggers` — чистая функция `(RiskMetrics, лимиты) → сработавшие триггеры`.
- `RiskMetricsBuilder` — метрики из SQLite (только чтение).
- `PostTradeMonitor` — после каждого fill: снимок портфеля → метрики → `CircuitBreaker.evaluate`.

Счётчики «серий» (убытки, невалидные fused_signal) и пик equity для просадки считаются
с момента последнего `cb reset` [ДОПУЩЕНИЕ]: иначе причина, уже разобранная человеком, сразу
снова сработала бы. Дневной убыток — по календарному UTC-дню (R10 и так блокирует входы).

Модуль не импортирует `augur.execution` (CB передаётся по протоколу) — нет цикла импортов.
"""

import json
import sqlite3
from collections.abc import Awaitable, Callable
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from augur.config.settings import CircuitBreakerSettings, FusionSettings, RiskLimits

__all__ = [
    "CBTrigger",
    "FillEvent",
    "PostTradeMonitor",
    "RiskMetrics",
    "RiskMetricsBuilder",
    "TriggerHit",
    "evaluate_triggers",
]


class CBTrigger(StrEnum):
    CONSECUTIVE_LOSSES = "consecutive_losses"
    DRAWDOWN = "drawdown"
    DAILY_LOSS = "daily_loss"
    ADAPTER_ERRORS = "adapter_errors"
    FUSION_INVALID_STREAK = "fusion_invalid_streak"
    RECONCILE_MISMATCH = "reconcile_mismatch"
    MANUAL = "manual"


class TriggerHit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger: CBTrigger
    detail: str


class RiskMetrics(BaseModel):
    """Вход CB. None — данных нет (триггер по этой метрике не срабатывает)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    consecutive_losses: int = 0
    drawdown_pct: Decimal | None = None  # доля от пика
    day_pnl: Decimal | None = None
    equity_start_of_day: Decimal | None = None
    adapter_errors_in_window: int = 0
    fatal_adapter_error: bool = False
    fusion_invalid_streak: int = 0
    fusion_invalid_ratio: Decimal | None = None  # None — окно ещё не набрано
    reconcile_mismatch: bool = False


class FillEvent(BaseModel):
    """Новый fill, применённый к позиции (OrderManager → PostTradeMonitor)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    realized_pnl: Decimal
    trade_closed: bool
    ts: int


def evaluate_triggers(
    m: RiskMetrics,
    *,
    limits: RiskLimits,
    cb: CircuitBreakerSettings,
    fusion: FusionSettings,
) -> list[TriggerHit]:
    """Сработавшие триггеры в фиксированном порядке ARCH §7.4 (пусто — CB не трогать)."""
    hits: list[TriggerHit] = []
    if m.consecutive_losses >= limits.max_consecutive_losses:
        hits.append(
            TriggerHit(
                trigger=CBTrigger.CONSECUTIVE_LOSSES,
                detail=f"{m.consecutive_losses} losses in a row >= {limits.max_consecutive_losses}",
            )
        )
    if m.drawdown_pct is not None and m.drawdown_pct > limits.max_drawdown_pct:
        hits.append(
            TriggerHit(
                trigger=CBTrigger.DRAWDOWN,
                detail=f"drawdown {m.drawdown_pct} > {limits.max_drawdown_pct}",
            )
        )
    if (
        m.day_pnl is not None
        and m.equity_start_of_day is not None
        and m.day_pnl < -limits.max_daily_loss_pct * m.equity_start_of_day
    ):
        hits.append(
            TriggerHit(
                trigger=CBTrigger.DAILY_LOSS,
                detail=(
                    f"day_pnl {m.day_pnl} < -{limits.max_daily_loss_pct} x {m.equity_start_of_day}"
                ),
            )
        )
    if m.fatal_adapter_error or m.adapter_errors_in_window >= cb.adapter_error_max:
        hits.append(
            TriggerHit(
                trigger=CBTrigger.ADAPTER_ERRORS,
                detail=(
                    "fatal adapter error"
                    if m.fatal_adapter_error
                    else f"{m.adapter_errors_in_window} errors in {cb.adapter_error_window_s}s"
                ),
            )
        )
    if m.fusion_invalid_streak >= fusion.max_invalid_streak or (
        m.fusion_invalid_ratio is not None and m.fusion_invalid_ratio > fusion.invalid_ratio
    ):
        hits.append(
            TriggerHit(
                trigger=CBTrigger.FUSION_INVALID_STREAK,
                detail=(
                    f"invalid streak {m.fusion_invalid_streak}, "
                    f"ratio {m.fusion_invalid_ratio} (window {fusion.invalid_window})"
                ),
            )
        )
    if m.reconcile_mismatch:
        hits.append(TriggerHit(trigger=CBTrigger.RECONCILE_MISMATCH, detail="position mismatch"))
    return hits


class RiskMetricsBuilder:
    """Метрики CB из SQLite: `audit_log` (закрытые сделки), `portfolio_snapshots`, `fused_signals`.

    Args:
        conn: соединение с миграциями 0005+.
        mode: режим, позиции/снимки которого оцениваются.
        fusion: окно для доли невалидных.
    """

    def __init__(self, conn: sqlite3.Connection, *, mode: str, fusion: FusionSettings) -> None:
        self._conn = conn
        self._mode = mode
        self._fusion = fusion

    def last_reset_ts(self) -> int:
        row = self._conn.execute("SELECT reset_ts FROM cb_state WHERE id = 1").fetchone()
        return int(row[0]) if row is not None and row[0] is not None else 0

    def consecutive_losses(self, since_ts: int) -> int:
        """Подряд идущие (с конца) закрытые сделки с PnL < 0.

        PnL сделки = realized PnL закрывающего fill (за вычетом комиссии) [ДОПУЩЕНИЕ].
        """
        rows = self._conn.execute(
            "SELECT payload_json FROM audit_log WHERE event = 'ORDER_UPDATED' AND ts >= ? "
            "AND json_extract(payload_json, '$.fill.trade_closed') = 1 "
            "AND json_extract(payload_json, '$.mode') = ? ORDER BY id DESC",
            (since_ts, self._mode),
        )
        n = 0
        for (payload,) in rows:
            pnl = Decimal(str(json.loads(payload)["fill"]["realized_pnl"]))
            if pnl >= 0:
                break
            n += 1
        return n

    def drawdown_since(
        self, since_ts: int
    ) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        """(drawdown от пика после `since_ts`, day_pnl, equity_start_of_day) по снимкам."""
        last = self._conn.execute(
            "SELECT equity, day_pnl FROM portfolio_snapshots WHERE mode = ? "
            "ORDER BY ts DESC LIMIT 1",
            (self._mode,),
        ).fetchone()
        if last is None:
            return None, None, None
        equity, day_pnl = Decimal(str(last[0])), Decimal(str(last[1]))
        peaks = self._conn.execute(
            "SELECT equity FROM portfolio_snapshots WHERE mode = ? AND ts >= ?",
            (self._mode, since_ts),
        ).fetchall()
        peak = max([Decimal(str(r[0])) for r in peaks] + [equity])
        dd = (peak - equity) / peak if peak > 0 else Decimal(0)
        return dd, day_pnl, equity - day_pnl

    def fusion_invalid(self, since_ts: int) -> tuple[int, Decimal | None]:
        """(серия невалидных с конца, доля невалидных в окне или None, если окно не набрано)."""
        rows = self._conn.execute(
            "SELECT valid FROM fused_signals WHERE created_at >= ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (since_ts, self._fusion.invalid_window),
        ).fetchall()
        streak = 0
        for (valid,) in rows:
            if int(valid) == 1:
                break
            streak += 1
        ratio = None
        if len(rows) >= self._fusion.invalid_window:
            ratio = Decimal(sum(1 for (v,) in rows if int(v) == 0)) / Decimal(len(rows))
        return streak, ratio

    def build(self) -> RiskMetrics:
        since = self.last_reset_ts()
        dd, day_pnl, sod = self.drawdown_since(since)
        streak, ratio = self.fusion_invalid(since)
        return RiskMetrics(
            consecutive_losses=self.consecutive_losses(since),
            drawdown_pct=dd,
            day_pnl=day_pnl,
            equity_start_of_day=sod,
            fusion_invalid_streak=streak,
            fusion_invalid_ratio=ratio,
        )


class _Breaker(Protocol):
    async def evaluate(self, metrics: RiskMetrics) -> TriggerHit | None: ...


class PostTradeMonitor:
    """После fill / обновления PnL пересчитывает метрики и передаёт их CB.

    Args:
        metrics: построитель метрик.
        breaker: `CircuitBreaker` (протокол `evaluate`).
        snapshot: корутина-фабрика, которая пишет снимок портфеля (`OrderManager.snapshot`).
    """

    def __init__(
        self,
        metrics: RiskMetricsBuilder,
        breaker: _Breaker,
        *,
        snapshot: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self._metrics = metrics
        self._breaker = breaker
        self._snapshot = snapshot

    async def on_fill(self, fill: FillEvent) -> TriggerHit | None:
        if self._snapshot is not None:
            await self._snapshot()
        return await self.on_pnl_update()

    async def on_pnl_update(self) -> TriggerHit | None:
        return await self._breaker.evaluate(self._metrics.build())
