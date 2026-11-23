"""Backtest Engine: equity, look-ahead, комиссии, проскальзывание, просадка, правила позиции."""

import random
from collections.abc import Callable, Sequence

import pytest
from pydantic import BaseModel, ConfigDict

from augur.backtest import Backtester
from augur.data.models import Bar
from augur.strategies import Signal, SignalAction, Strategy
from tests._bars import H, make_bars


class _NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FnStrategy(Strategy):
    """Тестовая стратегия из функции (не регистрируется в реестре)."""

    name = "fn"
    version = "fn_v1"
    params_model = _NoParams

    def __init__(
        self, fn: Callable[[Bar, Sequence[Bar]], SignalAction | None], warmup: int = 1
    ) -> None:
        super().__init__()
        self._fn = fn
        self._warmup = warmup

    @property
    def warmup_bars(self) -> int:
        return self._warmup

    def on_bar(self, bar: Bar, history: Sequence[Bar]) -> Signal | None:
        action = self._fn(bar, history)
        if action is None:
            return None
        return Signal(action=action, strength=1.0, reason="test", timestamp=bar.ts_open)


def buy_first_bar() -> FnStrategy:
    return FnStrategy(lambda bar, h: SignalAction.BUY if not h else None)


def script(actions: dict[int, SignalAction]) -> FnStrategy:
    """Сигналы по индексу бара (индекс = len(history))."""
    return FnStrategy(lambda bar, h: actions.get(len(h)))


CLOSES = [100.0, 120.0, 90.0, 95.0, 130.0, 104.0]


def test_buy_and_hold_equity_follows_price() -> None:
    bars = make_bars(CLOSES)
    res = Backtester().run(buy_first_bar(), bars, initial_cash=1000.0)
    assert [ts for ts, _ in res.equity_curve] == [b.ts_open for b in bars]
    for (_, eq), close in zip(res.equity_curve, CLOSES, strict=True):
        assert eq == pytest.approx(1000.0 * close / CLOSES[0])
    assert res.metrics["total_return"] == pytest.approx(0.04)
    # Позиция закрыта на последнем баре.
    assert len(res.trades) == 1 and res.trades[0].exit_reason == "end_of_data"
    assert res.trades[0].pnl == pytest.approx(40.0)


def test_max_drawdown_on_known_series() -> None:
    res = Backtester().run(buy_first_bar(), make_bars(CLOSES), initial_cash=1000.0)
    assert res.metrics["max_drawdown"] == pytest.approx(0.25)  # пик 120 → 90


def test_commission_reduces_result() -> None:
    bars = make_bars(CLOSES)
    free = Backtester().run(buy_first_bar(), bars, initial_cash=1000.0)
    paid = Backtester().run(buy_first_bar(), bars, initial_cash=1000.0, commission_pct=1.0)
    final_free, final_paid = free.equity_curve[-1][1], paid.equity_curve[-1][1]
    assert final_paid < final_free
    # qty = cash / (p0 × 1.01); выход: qty × pN × 0.99
    assert final_paid == pytest.approx(1000.0 * 104 / 100 * 0.99 / 1.01)
    qty = 1000.0 / (100 * 1.01)
    assert paid.trades[0].commission == pytest.approx(qty * 100 * 0.01 + qty * 104 * 0.01)


def test_slippage_worsens_fills() -> None:
    res = Backtester().run(
        buy_first_bar(), make_bars(CLOSES), initial_cash=1000.0, slippage_pct=0.5
    )
    t = res.trades[0]
    assert t.entry_price == pytest.approx(100.0 * 1.005)
    assert t.exit_price == pytest.approx(104.0 * 0.995)


def test_next_open_fills_on_following_bar() -> None:
    opens = [100.0, 101.0, 119.0, 91.0, 96.0, 129.0]
    bars = make_bars(CLOSES, opens=opens)
    res = Backtester().run(
        script({0: SignalAction.BUY, 3: SignalAction.CLOSE}), bars, fill_on="next_open"
    )
    t = res.trades[0]
    assert (t.entry_ts, t.entry_price) == (bars[1].ts_open, 101.0)
    assert (t.exit_ts, t.exit_price, t.exit_reason) == (bars[4].ts_open, 96.0, "signal")


def test_signal_on_last_bar_with_next_open_is_dropped() -> None:
    res = Backtester().run(script({5: SignalAction.BUY}), make_bars(CLOSES), fill_on="next_open")
    assert res.trades == []
    assert res.metrics["total_return"] == 0.0


# --- look-ahead ----------------------------------------------------------------


def test_strategy_sees_only_past_and_current_bar() -> None:
    bars = make_bars(CLOSES)
    seen: list[tuple[int, list[int]]] = []

    def record(bar: Bar, history: Sequence[Bar]) -> None:
        seen.append((bar.ts_open, [b.ts_open for b in history]))

    Backtester().run(FnStrategy(record), bars)  # type: ignore[arg-type]
    for i, (ts, hist) in enumerate(seen):
        assert ts == bars[i].ts_open
        assert hist == [b.ts_open for b in bars[:i]]


def test_tampering_with_history_does_not_leak_or_corrupt() -> None:
    bars = make_bars(CLOSES)

    def vandal(bar: Bar, history: Sequence[Bar]) -> SignalAction | None:
        if isinstance(history, list):
            history.clear()  # копия: данные движка не страдают
        return SignalAction.BUY if bar.ts_open == bars[0].ts_open else None

    res = Backtester().run(FnStrategy(vandal), bars, initial_cash=1000.0)
    assert res.equity_curve[-1][1] == pytest.approx(1040.0)


def _random_walk(n: int, seed: int) -> list[float]:
    rng = random.Random(seed)  # noqa: S311
    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.uniform(-0.02, 0.02)))
    return closes


def test_future_peeking_strategy_gains_nothing() -> None:
    """«Оракул» торгует, только если найдёт следующий бар; без утечки — сделок нет.

    Тот же оракул с реальным доступом к будущему (через замыкание) даёт аномальную
    доходность — значит, тест действительно ловит утечку.
    """
    bars = make_bars(_random_walk(300, seed=11))

    def oracle_from(source: Sequence[Bar]) -> Callable[[Bar, Sequence[Bar]], SignalAction | None]:
        def fn(bar: Bar, history: Sequence[Bar]) -> SignalAction | None:
            nxt = next((b for b in source if b.ts_open == bar.ts_open + H), None)
            if nxt is None:
                return None
            return SignalAction.BUY if nxt.close > bar.close else SignalAction.CLOSE

        return fn

    honest = Backtester().run(FnStrategy(lambda bar, h: oracle_from(h)(bar, h)), bars)
    assert honest.trades == []
    assert honest.metrics["total_return"] == 0.0

    cheating = Backtester().run(FnStrategy(oracle_from(bars)), bars)
    assert cheating.metrics["total_return"] > 1.0  # >100 % на случайном блуждании


# --- правила позиции -----------------------------------------------------------


def test_short_position_and_no_flip() -> None:
    closes = [100.0, 90.0, 80.0, 85.0, 70.0]
    res = Backtester().run(
        script({0: SignalAction.SELL, 1: SignalAction.BUY, 2: SignalAction.CLOSE}),
        make_bars(closes),
        initial_cash=1000.0,
    )
    assert len(res.trades) == 1  # BUY при открытом short проигнорирован
    t = res.trades[0]
    assert t.side == "SHORT" and t.exit_reason == "signal"
    assert t.pnl == pytest.approx(10.0 * 20.0)  # qty = 10, 100 → 80
    assert res.equity_curve[1][1] == pytest.approx(1100.0)
    assert res.equity_curve[-1][1] == pytest.approx(1200.0)


def test_reduce_halves_position_and_hold_does_nothing() -> None:
    res = Backtester().run(
        script({0: SignalAction.BUY, 1: SignalAction.REDUCE, 2: SignalAction.HOLD}),
        make_bars([100.0, 110.0, 120.0, 130.0]),
        initial_cash=1000.0,
    )
    assert [(t.qty, t.exit_reason) for t in res.trades] == [
        (pytest.approx(5.0), "reduce"),
        (pytest.approx(5.0), "end_of_data"),
    ]
    assert res.equity_curve[-1][1] == pytest.approx(1000 + 5 * 10 + 5 * 30)


def test_warmup_skips_early_bars() -> None:
    calls: list[int] = []

    def count(bar: Bar, history: Sequence[Bar]) -> None:
        calls.append(len(history))

    Backtester().run(FnStrategy(count, warmup=4), make_bars(CLOSES))  # type: ignore[arg-type]
    assert calls == [3, 4, 5]


@pytest.mark.parametrize(
    "bars",
    [
        [],
        list(reversed(make_bars(CLOSES))),
        [b.model_copy(update={"is_closed": False}) for b in make_bars(CLOSES)],
        make_bars(CLOSES) + make_bars([1.0], symbol="OTHER"),
    ],
)
def test_invalid_data_rejected(bars: list[Bar]) -> None:
    with pytest.raises(ValueError):
        Backtester().run(buy_first_bar(), bars)


def test_invalid_costs_rejected() -> None:
    with pytest.raises(ValueError):
        Backtester().run(buy_first_bar(), make_bars(CLOSES), commission_pct=-1)
    with pytest.raises(ValueError):
        Backtester().run(buy_first_bar(), make_bars(CLOSES), initial_cash=0)
