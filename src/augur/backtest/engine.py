"""Событийный (bar-by-bar) бэктест одной стратегии на одном инструменте (ARCHITECTURE.md §6.3).

Этап 0.3.0 — без Risk Gate / PaperBroker / Fusion: только стратегия, издержки и метрики.

Правила:
- Look-ahead запрещён: на баре i стратегия получает `bar = data[i]` (закрытый) и
  `history = data[:i]` — новую последовательность без будущих баров.
- Исполнение: `fill_on="close"` — по close бара сигнала (по умолчанию, ТЗ 0.3.0);
  `fill_on="next_open"` — по open следующего бара (ARCHITECTURE.md §6.3).
- Проскальзывание: покупка по `price × (1 + s)`, продажа по `price × (1 − s)`.
  Комиссия: `notional × c`, вычитается из cash. `s`, `c` задаются в процентах.
  Модель общая с PaperAdapter — `augur.backtest.costs.CostModel` (0.6.0).
- Одна позиция: на весь доступный капитал. BUY открывает long, SELL — short, только из
  flat; противоположный сигнал при открытой позиции игнорируется (long и short не
  смешиваются, переворота нет — сначала CLOSE). REDUCE закрывает половину позиции.
  HOLD / NO_SIGNAL — ничего. Открытая позиция закрывается на последнем баре.
- Equity — по close каждого бара (mark-to-market), точка = `ts_open` бара.
"""

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from augur.backtest.costs import CostModel
from augur.backtest.metrics import compute_metrics
from augur.data.models import Bar, timeframe_ms
from augur.strategies.base import Signal, SignalAction, Strategy

FillOn = Literal["close", "next_open"]
Side = Literal["LONG", "SHORT"]

YEAR_MS = 365 * 24 * 60 * 60 * 1000  # аннуализация по календарному году (крипта 24/7)


class Trade(BaseModel):
    """Сделка (вход + выход). `pnl` — чистый, с комиссиями обеих сторон."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    side: Side
    qty: float
    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    commission: float
    pnl: float
    return_pct: float  # pnl / (entry_notional + entry_commission), доля
    exit_reason: str


class BacktestResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: str
    strategy_version: str
    params: dict[str, object]
    equity_curve: list[tuple[int, float]]
    trades: list[Trade]
    metrics: dict[str, float]


class _Position:
    def __init__(self, side: Side, qty: float, price: float, ts: int, commission: float) -> None:
        self.side: Side = side
        self.qty = qty
        self.entry_price = price
        self.entry_ts = ts
        self.entry_commission = commission  # остаток комиссии входа на текущий qty


def _validate_data(data: Sequence[Bar]) -> None:
    if not data:
        raise ValueError("empty data")
    first = data[0]
    prev_ts = -1
    for bar in data:
        if bar.symbol != first.symbol or bar.timeframe != first.timeframe:
            raise ValueError("data must contain one symbol and one timeframe")
        if not bar.is_closed:
            raise ValueError(f"unclosed bar at ts_open={bar.ts_open}")
        if bar.ts_open <= prev_ts:
            raise ValueError("bars must be strictly increasing by ts_open")
        prev_ts = bar.ts_open


class Backtester:
    """Прогон стратегии по истории без доступа к будущему."""

    def run(
        self,
        strategy: Strategy,
        data: Sequence[Bar],
        initial_cash: float = 10_000.0,
        commission_pct: float = 0.0,
        slippage_pct: float = 0.0,
        *,
        fill_on: FillOn = "close",
    ) -> BacktestResult:
        """
        Args:
            strategy: экземпляр стратегии.
            data: закрытые бары одного инструмента, по возрастанию `ts_open`.
            initial_cash: стартовый капитал (> 0).
            commission_pct: комиссия в процентах от notional (0.1 = 0.1 %).
            slippage_pct: проскальзывание в процентах от цены.
            fill_on: "close" — по close бара сигнала; "next_open" — по open следующего бара.

        Raises:
            ValueError: некорректные данные или параметры.
        """
        _validate_data(data)
        if initial_cash <= 0:
            raise ValueError("initial_cash must be > 0")
        if not 0 <= commission_pct < 100 or not 0 <= slippage_pct < 100:
            raise ValueError("commission_pct and slippage_pct must be in [0, 100)")
        if fill_on not in ("close", "next_open"):
            raise ValueError(f"unsupported fill_on: {fill_on!r}")

        self._costs = CostModel.from_floats(commission_pct, slippage_pct)
        self._c = self._costs.commission_rate
        self._cash = float(initial_cash)
        self._pos: _Position | None = None
        self._trades: list[Trade] = []

        equity_curve: list[tuple[int, float]] = []
        pending: Signal | None = None
        warmup = max(1, strategy.warmup_bars)
        last = len(data) - 1

        for i, bar in enumerate(data):
            if pending is not None:  # fill_on="next_open": сигнал с бара i-1
                self._apply(pending, bar.open, bar.ts_open)
                pending = None

            if i + 1 >= warmup:
                signal = strategy.on_bar(bar, list(data[:i]))
                if signal is not None:
                    if fill_on == "close":
                        self._apply(signal, bar.close, bar.ts_open)
                    elif i < last:
                        pending = signal

            if i == last and self._pos is not None:
                self._close(bar.close, bar.ts_open, self._pos.qty, "end_of_data")

            equity_curve.append((bar.ts_open, self._equity(bar.close)))

        periods_per_year = YEAR_MS / timeframe_ms(data[0].timeframe)
        metrics = compute_metrics(
            [e for _, e in equity_curve], [t.pnl for t in self._trades], periods_per_year
        )
        return BacktestResult(
            strategy=strategy.name,
            strategy_version=strategy.strategy_version,
            params=strategy.params,
            equity_curve=equity_curve,
            trades=self._trades,
            metrics=metrics,
        )

    # --- исполнение -----------------------------------------------------------

    def _equity(self, price: float) -> float:
        pos = self._pos
        if pos is None:
            return self._cash
        if pos.side == "LONG":
            return self._cash + pos.qty * price
        return self._cash - pos.qty * price

    def _apply(self, signal: Signal, price: float, ts: int) -> None:
        action = signal.action
        pos = self._pos
        if action in (SignalAction.BUY, SignalAction.SELL):
            if pos is None:
                self._open("LONG" if action == SignalAction.BUY else "SHORT", price, ts)
            # иначе: позиция уже есть (та же или противоположная) — игнор, без переворота
        elif action == SignalAction.CLOSE:
            if pos is not None:
                self._close(price, ts, pos.qty, "signal")
        elif action == SignalAction.REDUCE:
            if pos is not None:
                self._close(price, ts, pos.qty / 2.0, "reduce")
        # HOLD / NO_SIGNAL — ничего

    def _open(self, side: Side, price: float, ts: int) -> None:
        fill = self._costs.fill_price_f("BUY" if side == "LONG" else "SELL", price)
        qty = self._cash / (fill * (1 + self._c))
        if qty <= 0:
            return
        notional = qty * fill
        commission = self._costs.fee_f(notional)
        if side == "LONG":
            self._cash -= notional + commission
        else:
            self._cash += notional - commission
        self._pos = _Position(side, qty, fill, ts, commission)

    def _close(self, price: float, ts: int, qty: float, reason: str) -> None:
        pos = self._pos
        if pos is None or qty <= 0:
            return
        qty = min(qty, pos.qty)
        fill = self._costs.fill_price_f("SELL" if pos.side == "LONG" else "BUY", price)
        notional = qty * fill
        exit_commission = self._costs.fee_f(notional)
        share = qty / pos.qty
        entry_commission = pos.entry_commission * share
        if pos.side == "LONG":
            self._cash += notional - exit_commission
            gross = (fill - pos.entry_price) * qty
        else:
            self._cash -= notional + exit_commission
            gross = (pos.entry_price - fill) * qty
        commission = entry_commission + exit_commission
        pnl = gross - commission
        cost_basis = pos.entry_price * qty + entry_commission
        self._trades.append(
            Trade(
                side=pos.side,
                qty=qty,
                entry_ts=pos.entry_ts,
                entry_price=pos.entry_price,
                exit_ts=ts,
                exit_price=fill,
                commission=commission,
                pnl=pnl,
                return_pct=pnl / cost_basis if cost_basis else 0.0,
                exit_reason=reason,
            )
        )
        remaining = pos.qty - qty
        if remaining <= pos.qty * 1e-12:
            self._pos = None
        else:
            pos.qty = remaining
            pos.entry_commission -= entry_commission
