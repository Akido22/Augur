"""Стратегии SMA Crossover и RSI на синтетических данных; реестр; отсутствие look-ahead."""

import pytest
from pydantic import ValidationError

from augur.adapters import PaperAdapter
from augur.data.models import Bar
from augur.strategies import (
    Signal,
    SignalAction,
    Strategy,
    StrategyParamsError,
    UnknownStrategyError,
    get_strategy,
    list_strategies,
)
from augur.strategies.indicators import rsi, sma
from tests._bars import T0, H, make_bars


def signals(strategy: Strategy, bars: list[Bar]) -> list[tuple[int, Signal]]:
    """Прогон стратегии так же, как в бэктесте: bar = bars[i], history = bars[:i]."""
    out: list[tuple[int, Signal]] = []
    for i, bar in enumerate(bars):
        s = strategy.on_bar(bar, bars[:i])
        if s is not None:
            out.append((i, s))
    return out


# --- Signal / реестр -----------------------------------------------------------


def test_signal_validates_strength_and_is_frozen() -> None:
    s = Signal(action=SignalAction.BUY, strength=0.5, reason="x", timestamp=1)
    with pytest.raises(ValidationError):
        Signal(action=SignalAction.BUY, strength=1.5, reason="x", timestamp=1)
    with pytest.raises(ValidationError):
        s.strength = 0.1  # type: ignore[misc]
    assert {a.value for a in SignalAction} == {
        "BUY",
        "SELL",
        "CLOSE",
        "REDUCE",
        "HOLD",
        "NO_SIGNAL",
    }


def test_registry_lists_and_creates_strategies() -> None:
    by_name = {s["name"]: s for s in list_strategies()}
    assert by_name["sma_cross"]["params"] == {"short": 10, "long": 30}
    assert by_name["sma_cross"]["warmup_bars"] == 31
    assert by_name["sma_cross"]["version"] == "1.0.0"
    assert by_name["sma_cross"]["strategy_version"] == "sma_cross@1.0.0"
    assert by_name["rsi"]["strategy_version"] == "rsi@1.0.0"
    assert by_name["rsi"]["params"] == {"period": 14, "oversold": 30.0, "overbought": 70.0}
    strat = get_strategy("sma_cross", {"short": "5", "long": "20"})  # строки из CLI
    assert strat.params == {"short": 5, "long": 20}
    assert strat.warmup_bars == 21


def test_registry_errors() -> None:
    with pytest.raises(UnknownStrategyError):
        get_strategy("nope")
    with pytest.raises(StrategyParamsError):
        get_strategy("sma_cross", {"short": 30, "long": 10})
    with pytest.raises(StrategyParamsError):
        get_strategy("sma_cross", {"shrot": 5})  # опечатка в ключе не проходит молча
    with pytest.raises(StrategyParamsError):
        get_strategy("rsi", {"oversold": 80, "overbought": 70})


# --- индикаторы ----------------------------------------------------------------


def test_sma() -> None:
    assert sma([1, 2, 3, 4], 2) == 3.5
    assert sma([1, 2], 3) is None


def test_rsi_reference_value() -> None:
    # изменения +1, −1, +2; period=2: avg_gain 0.5→1.25, avg_loss 0.5→0.25 → RS=5
    assert rsi([10, 11, 10, 12], 2) == pytest.approx(100 - 100 / 6)
    assert rsi([10, 11], 2) is None
    assert rsi([5, 5, 5, 5], 2) == 50.0
    assert rsi([1, 2, 3, 4], 2) == 100.0
    assert rsi([4, 3, 2, 1], 2) == 0.0


# --- SMA Crossover -------------------------------------------------------------


def test_sma_buy_on_upward_cross() -> None:
    bars = make_bars([10, 10, 10, 10, 10, 12])
    out = signals(get_strategy("sma_cross", {"short": 2, "long": 4}), bars)
    assert [(i, s.action) for i, s in out] == [(5, SignalAction.BUY)]
    assert out[0][1].timestamp == bars[5].ts_open
    assert "crossed above" in out[0][1].reason


def test_sma_close_on_downward_cross() -> None:
    out = signals(get_strategy("sma_cross", {"short": 2, "long": 4}), make_bars([10] * 5 + [8]))
    assert [(i, s.action) for i, s in out] == [(5, SignalAction.CLOSE)]


def test_sma_no_signal_without_cross() -> None:
    # Монотонный рост: короткая SMA выше длинной с первого же окна — пересечения нет.
    out = signals(get_strategy("sma_cross", {"short": 2, "long": 4}), make_bars(range(1, 30)))
    assert out == []


def test_sma_round_trip_and_warmup() -> None:
    closes = [10] * 5 + [12, 14, 14, 10, 6, 6]
    strat = get_strategy("sma_cross", {"short": 2, "long": 4})
    out = signals(strat, make_bars(closes))
    assert [s.action for _, s in out] == [SignalAction.BUY, SignalAction.CLOSE]
    # Меньше warmup_bars баров — решения нет.
    bars = make_bars([10, 10, 10, 12])
    assert strat.on_bar(bars[-1], bars[:-1]) is None


# --- RSI -----------------------------------------------------------------------


def test_rsi_buy_when_oversold() -> None:
    out = signals(get_strategy("rsi", {"period": 2}), make_bars([100 - i for i in range(12)]))
    assert out and all(s.action == SignalAction.BUY for _, s in out)
    assert out[0][0] == 8  # первый бар после warmup (4 × period + 1 = 9 баров)
    assert out[0][1].strength == 1.0  # RSI = 0


def test_rsi_close_when_overbought() -> None:
    out = signals(get_strategy("rsi", {"period": 2}), make_bars([100 + i for i in range(12)]))
    assert out and all(s.action == SignalAction.CLOSE for _, s in out)


def test_rsi_no_signal_in_neutral_zone() -> None:
    assert signals(get_strategy("rsi", {"period": 2}), make_bars([100.0] * 20)) == []
    # Чередование ±1 держит RSI около 50.
    zigzag = [100 + (i % 2) for i in range(30)]
    assert signals(get_strategy("rsi", {"period": 3}), make_bars(zigzag)) == []


def test_rsi_custom_levels() -> None:
    # +2, −1 по кругу, period=14: RSI ∈ [65, 68.2] — выше 60 (CLOSE), но ниже дефолтных 70.
    closes = [100.0]
    for i in range(80):
        closes.append(closes[-1] + (2 if i % 2 == 0 else -1))
    bars = make_bars(closes)
    default = get_strategy("rsi", {"period": 14})
    assert all(s.action != SignalAction.CLOSE for _, s in signals(default, bars))
    tight = get_strategy("rsi", {"period": 14, "oversold": 40, "overbought": 60})
    assert any(s.action == SignalAction.CLOSE for _, s in signals(tight, bars))


# --- look-ahead ----------------------------------------------------------------


@pytest.mark.parametrize("name,params", [("sma_cross", {"short": 5, "long": 20}), ("rsi", {})])
async def test_signals_do_not_depend_on_future_bars(name: str, params: dict[str, int]) -> None:
    """Сигналы на общем префиксе совпадают при полном и обрезанном наборе (ARCH §6.3 п.3)."""
    adapter = PaperAdapter(seed=3, clock_ms=T0 + 1000 * H)
    full = await adapter.get_historical("BTC/USDT", "1h", T0, T0 + 600 * H)
    cut = 400
    strat = get_strategy(name, params)
    on_full = [(i, s) for i, s in signals(strat, full) if i < cut]
    assert on_full == signals(strat, full[:cut])
    assert on_full  # на синтетике есть хотя бы один сигнал — тест не пустой
