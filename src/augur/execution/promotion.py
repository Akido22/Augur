"""Критерии promotion MICRO_LIVE → FULL_AUTO (0.11.0; ARCHITECTURE.md §1.4, §7.1, §15 п. 1, 10).

Все условия сразу (решения пользователя 2026-11-28):
1. С момента входа в MICRO_LIVE исполнено (`FILLED`) не менее `mode.min_micro_trades` ордеров
   MICRO_LIVE, и за это время не было `CB_TRIPPED` / `KILL_SWITCH`.
2. Сверка исполнения с общей с бэктестом `CostModel` (ARCH §15 п. 10): для каждого сигнального
   FILLED-ордера MICRO_LIVE опорная цена — close бара сигнала (`bar_ts` родительского сигнала
   стратегии, как в бэктесте с `fill_on=close`). Модель: `CostModel.fill_price(side, close)`
   и `CostModel.fee(notional)`.
   Перекос ордера, б.п. (плюс — хуже модели): проскальзывание против стороны + лишняя комиссия
   к notional. Нужно не меньше `mode.min_compared_orders` сравнимых ордеров и средний перекос
   ≤ `mode.max_cost_excess_bps`. Системные выходы (SL/TP/flatten) без бара не сравниваются;
   ордер без бара в `ohlcv` — несравним (fail-closed: он не засчитывается).
3. Нет активных (не терминальных) ордеров MICRO_LIVE: книга позиций переносится в FULL_AUTO
   одной транзакцией со сменой режима (`ModeStore`).
4. Канал алертов настроен (`mode.require_alerts`): без человека в контуре о CB и kill switch
   должен узнать хотя бы Telegram.
Утверждает один человек: `augur mode set FULL_AUTO --approved-by <имя> --confirm` (код).
"""

import sqlite3
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict

from augur.backtest.costs import CostModel, OrderSide
from augur.config.settings import Settings, TradingMode
from augur.execution.state_machine import TERMINAL_STATES

__all__ = ["OrderCostCheck", "PromotionStatus", "alerts_configured", "promotion_status"]

_BPS = Decimal(10_000)
_TERMINAL = tuple(s.value for s in TERMINAL_STATES)


class OrderCostCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str
    side: str
    ref_price: Decimal
    model_price: Decimal
    fill_price: Decimal
    slippage_excess_bps: Decimal
    fee_excess_bps: Decimal

    @property
    def cost_excess_bps(self) -> Decimal:
        return self.slippage_excess_bps + self.fee_excess_bps


class PromotionStatus(BaseModel):
    """Состояние критериев FULL_AUTO. `ok` — все выполнены; `reasons()` — что не так."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    micro_trades: int
    required: int
    cb_tripped_since_entry: bool
    in_micro_live: bool = False
    since_ts: int | None = None
    compared_orders: int = 0
    required_compared: int = 0
    uncomparable_orders: int = 0
    mean_cost_excess_bps: Decimal | None = None
    max_cost_excess_bps: Decimal = Decimal(0)
    active_orders: int = 0
    alerts_configured: bool = True
    require_alerts: bool = False
    checks: list[OrderCostCheck] = []

    def reasons(self) -> list[str]:
        out: list[str] = []
        if not self.in_micro_live:
            out.append("current stored mode is not MICRO_LIVE")
        if self.micro_trades < self.required:
            out.append(f"MICRO_LIVE trades {self.micro_trades}/{self.required}")
        if self.cb_tripped_since_entry:
            out.append("CB_TRIPPED or KILL_SWITCH since entering MICRO_LIVE")
        if self.compared_orders < self.required_compared:
            out.append(
                f"orders compared with CostModel {self.compared_orders}/{self.required_compared}"
                f" (uncomparable: {self.uncomparable_orders})"
            )
        if self.mean_cost_excess_bps is not None and (
            self.mean_cost_excess_bps > self.max_cost_excess_bps
        ):
            out.append(
                f"mean cost excess {self.mean_cost_excess_bps:.2f} bps > "
                f"{self.max_cost_excess_bps} bps"
            )
        if self.active_orders:
            out.append(f"{self.active_orders} active MICRO_LIVE orders (wait until terminal)")
        if self.require_alerts and not self.alerts_configured:
            out.append("alerts not configured (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)")
        return out

    @property
    def ok(self) -> bool:
        return not self.reasons()

    def summary(self) -> dict[str, object]:
        """Для `audit_log` (без списка ордеров)."""
        return {
            "micro_trades": self.micro_trades,
            "required": self.required,
            "cb_tripped_since_entry": self.cb_tripped_since_entry,
            "compared_orders": self.compared_orders,
            "uncomparable_orders": self.uncomparable_orders,
            "mean_cost_excess_bps": None
            if self.mean_cost_excess_bps is None
            else str(self.mean_cost_excess_bps.quantize(Decimal("0.01"))),
            "max_cost_excess_bps": str(self.max_cost_excess_bps),
            "active_orders": self.active_orders,
            "alerts_configured": self.alerts_configured,
        }


def alerts_configured(settings: Settings) -> bool:
    return (
        settings.notify.enabled
        and settings.telegram_bot_token is not None
        and bool(settings.telegram_bot_token.get_secret_value())
        and bool(settings.telegram_chat_id)
    )


def _dec(v: object) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _cost_check(
    conn: sqlite3.Connection,
    costs: CostModel,
    row: tuple[object, ...],
    venues: tuple[str, ...],
) -> OrderCostCheck | None:
    order_id, side, filled, avg, fee, symbol, timeframe, bar_ts = row
    qty, fill, fee_d = _dec(filled), _dec(avg), _dec(fee) or Decimal(0)
    if qty is None or fill is None or qty <= 0 or fill <= 0 or timeframe is None:
        return None
    ref: Decimal | None = None
    for venue in venues:
        r = conn.execute(
            "SELECT close FROM ohlcv WHERE venue = ? AND symbol = ? AND timeframe = ? "
            "AND ts_open = ?",
            (venue, symbol, timeframe, bar_ts),
        ).fetchone()
        if r is not None:
            ref = _dec(r[0])
            break
    if ref is None or ref <= 0:
        return None
    s: OrderSide = "BUY" if side == "BUY" else "SELL"
    model = costs.fill_price(s, ref)
    slip = (fill - model) / model * _BPS if s == "BUY" else (model - fill) / model * _BPS
    notional = fill * qty
    fee_x = (fee_d - costs.fee(notional)) / notional * _BPS
    return OrderCostCheck(
        order_id=str(order_id),
        side=s,
        ref_price=ref,
        model_price=model,
        fill_price=fill,
        slippage_excess_bps=slip,
        fee_excess_bps=fee_x,
    )


def promotion_status(conn: sqlite3.Connection, settings: Settings) -> PromotionStatus:
    """Посчитать критерии FULL_AUTO по БД. Чтение, без побочных эффектов."""
    m = settings.mode
    base = {
        "required": m.min_micro_trades,
        "required_compared": m.min_compared_orders,
        "max_cost_excess_bps": m.max_cost_excess_bps,
        "alerts_configured": alerts_configured(settings),
        "require_alerts": m.require_alerts,
    }
    row = conn.execute("SELECT mode, changed_ts FROM mode_state WHERE id = 1").fetchone()
    if row is None or row[0] != TradingMode.MICRO_LIVE.value:
        return PromotionStatus(micro_trades=0, cb_tripped_since_entry=False, **base)  # type: ignore[arg-type]
    since = int(row[1])
    live = TradingMode.MICRO_LIVE.value
    trades = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE mode = ? AND state = 'FILLED' AND created_ts >= ?",
        (live, since),
    ).fetchone()[0]
    tripped = conn.execute(
        "SELECT 1 FROM audit_log WHERE event IN ('CB_TRIPPED', 'KILL_SWITCH') AND ts >= ? LIMIT 1",
        (since,),
    ).fetchone()
    placeholders = ",".join("?" * len(_TERMINAL))
    active = conn.execute(
        f"SELECT COUNT(*) FROM orders WHERE mode = ? AND state NOT IN ({placeholders})",  # noqa: S608 — только плейсхолдеры
        (live, *_TERMINAL),
    ).fetchone()[0]
    # Итоговый сигнал не хранит бар: берём бар первой родительской стратегии (parent_ids_json).
    rows = conn.execute(
        "SELECT o.id, o.side, o.filled_qty, o.avg_fill_price, o.fee, o.symbol, "
        "COALESCE(s.timeframe, p.timeframe), COALESCE(s.bar_ts, p.bar_ts), o.venue "
        "FROM orders o JOIN signals s ON s.id = o.signal_id "
        "LEFT JOIN signals p ON p.id = json_extract(s.parent_ids_json, '$[0]') "
        "WHERE o.mode = ? AND o.state = 'FILLED' AND o.created_ts >= ? "
        "AND COALESCE(s.bar_ts, p.bar_ts) IS NOT NULL ORDER BY o.created_ts, o.id",
        (live, since),
    ).fetchall()
    ex = settings.execution
    costs = CostModel(commission_pct=ex.commission_pct, slippage_pct=ex.slippage_pct)
    checks: list[OrderCostCheck] = []
    for r in rows:
        venues = tuple(dict.fromkeys((str(r[8]), settings.fusion.ml.venue)))
        chk = _cost_check(conn, costs, r[:8], venues)
        if chk is not None:
            checks.append(chk)
    mean = sum((c.cost_excess_bps for c in checks), Decimal(0)) / len(checks) if checks else None
    return PromotionStatus(
        micro_trades=int(trades),
        cb_tripped_since_entry=tripped is not None,
        in_micro_live=True,
        since_ts=since,
        compared_orders=len(checks),
        uncomparable_orders=len(rows) - len(checks),
        mean_cost_excess_bps=mean,
        active_orders=int(active),
        checks=checks,
        **base,  # type: ignore[arg-type]
    )
