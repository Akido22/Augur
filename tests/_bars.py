"""Помощники для тестов стратегий и бэктеста: синтетические бары из списка цен."""

from collections.abc import Sequence

from augur.data.models import Bar, timeframe_ms

H = timeframe_ms("1h")
T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC


def make_bars(
    closes: Sequence[float], opens: Sequence[float] | None = None, symbol: str = "TEST/USD"
) -> list[Bar]:
    """Закрытые часовые бары; open по умолчанию = предыдущий close."""
    bars: list[Bar] = []
    for i, close in enumerate(closes):
        open_ = opens[i] if opens is not None else (closes[i - 1] if i else close)
        bars.append(
            Bar(
                symbol=symbol,
                venue="test",
                timeframe="1h",
                ts_open=T0 + i * H,
                open=open_,
                high=max(open_, close),
                low=min(open_, close),
                close=close,
                volume=1.0,
            )
        )
    return bars
