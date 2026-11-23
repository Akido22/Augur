"""Индикаторы на чистом Python (без numpy/pandas). Считаются на срезе — без look-ahead."""

from collections.abc import Sequence


def sma(values: Sequence[float], period: int) -> float | None:
    """Простое скользящее среднее последних `period` значений; None, если данных мало."""
    if period < 1:
        raise ValueError("period must be >= 1")
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(closes: Sequence[float], period: int) -> float | None:
    """RSI по Уайлдеру на всей переданной последовательности.

    Первое среднее — простое по первым `period` изменениям, далее сглаживание
    `avg = (avg * (period - 1) + x) / period`. Нужно минимум `period + 1` цен.

    Returns:
        Значение в [0, 100] или None, если данных мало. Без движения цены — 50.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    if len(closes) < period + 1:
        return None
    changes = [b - a for a, b in zip(closes[:-1], closes[1:], strict=True)]
    avg_gain = sum(max(c, 0.0) for c in changes[:period]) / period
    avg_loss = sum(max(-c, 0.0) for c in changes[:period]) / period
    for c in changes[period:]:
        avg_gain = (avg_gain * (period - 1) + max(c, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-c, 0.0)) / period
    if avg_loss == 0.0:
        return 50.0 if avg_gain == 0.0 else 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)
