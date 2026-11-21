"""DTO рыночных данных (ARCHITECTURE.md §2.3).

Время — int, Unix ms UTC (как в SQLite, ARCHITECTURE.md §3.1).
"""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


def timeframe_ms(timeframe: str) -> int:
    """Длительность таймфрейма в миллисекундах.

    Raises:
        ValueError: таймфрейм не поддерживается.
    """
    try:
        return TIMEFRAME_MS[timeframe]
    except KeyError as e:
        raise ValueError(f"unsupported timeframe: {timeframe!r}") from e


class Bar(BaseModel):
    """OHLCV-бар. `ts_open` — начало бара, Unix ms UTC."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str  # каноническое имя: "BTC/USDT", "EURUSD", "AAPL"
    venue: str  # "ccxt:binance" | "mt5" | "alpaca" | "paper"
    timeframe: str  # "1m","5m","15m","1h","4h","1d"
    ts_open: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool = True


class Tick(BaseModel):
    """Последняя котировка. `ts` — Unix ms UTC."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    venue: str
    ts: int
    bid: Decimal
    ask: Decimal
    last: Decimal | None = None
    volume: float | None = None


def bar_anomalies(bar: Bar) -> list[str]:
    """Проверки целостности бара (ARCHITECTURE.md §2.3).

    Returns:
        Список нарушений; пустой список — бар корректен.
    """
    problems: list[str] = []
    if bar.high < max(bar.open, bar.close):
        problems.append("high < max(open, close)")
    if bar.low > min(bar.open, bar.close):
        problems.append("low > min(open, close)")
    if bar.volume < 0:
        problems.append("volume < 0")
    if bar.ts_open < 0:
        problems.append("ts_open < 0")
    return problems
