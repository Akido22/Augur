"""PaperAdapter: детерминированная синтетика, мгновенное исполнение, идемпотентность."""

from decimal import Decimal

import pytest

from augur.adapters import MarketAdapter, OrderRequest, OrderState, PaperAdapter, RejectedError
from augur.data.models import bar_anomalies, timeframe_ms

H = timeframe_ms("1h")
T0 = 1_795_000_000_000 // H * H  # граница часа
NOW = T0 + 48 * H


def _adapter(**kw: object) -> PaperAdapter:
    return PaperAdapter(clock_ms=NOW, **kw)  # type: ignore[arg-type]


def test_implements_protocol() -> None:
    assert isinstance(_adapter(), MarketAdapter)


async def test_synthetic_bars_are_valid_aligned_and_deterministic() -> None:
    a, b = _adapter(seed=7), _adapter(seed=7)
    bars = await a.get_historical("BTC/USDT", "1h", T0, T0 + 24 * H)
    assert len(bars) == 24
    assert [x.ts_open for x in bars] == [T0 + i * H for i in range(24)]
    assert all(not bar_anomalies(x) for x in bars)
    assert all(x.is_closed for x in bars)
    assert bars == await b.get_historical("BTC/USDT", "1h", T0, T0 + 24 * H)
    other = await _adapter(seed=8).get_historical("BTC/USDT", "1h", T0, T0 + 24 * H)
    assert bars != other


async def test_range_is_half_open_and_aligned_up() -> None:
    bars = await _adapter().get_historical("BTC/USDT", "1h", T0 + 1, T0 + 3 * H)
    assert [x.ts_open for x in bars] == [T0 + H, T0 + 2 * H]


async def test_current_bar_is_not_closed() -> None:
    bars = await _adapter().get_historical("BTC/USDT", "1h", NOW - H, NOW + H)
    assert [x.is_closed for x in bars] == [True, False]


async def test_place_order_fills_at_current_price() -> None:
    a = _adapter()
    tick = await a.get_latest("BTC/USDT")
    ack = await a.place_order(
        OrderRequest(
            client_order_id="c1", symbol="BTC/USDT", side="BUY", type="MARKET", qty=Decimal("2")
        )
    )
    assert ack.state is OrderState.FILLED
    assert ack.filled_qty == Decimal("2")
    assert ack.avg_price == tick.ask
    [pos] = await a.get_positions()
    assert pos.symbol == "BTC/USDT" and pos.qty == Decimal("2") and pos.avg_price == tick.ask
    acct = await a.get_account_state()
    assert acct.cash == Decimal("10000") - (tick.ask * 2).quantize(Decimal("0.00000001"))


async def test_place_order_is_idempotent_by_client_order_id() -> None:
    a = _adapter()
    req = OrderRequest(
        client_order_id="dup", symbol="BTC/USDT", side="BUY", type="MARKET", qty=Decimal("1")
    )
    first = await a.place_order(req)
    second = await a.place_order(req)
    assert first == second
    [pos] = await a.get_positions()
    assert pos.qty == Decimal("1")  # одна сделка, не две
    assert await a.get_order("dup", "BTC/USDT") == first


async def test_sell_closes_position_and_insufficient_funds_rejected() -> None:
    a = _adapter(start_cash=Decimal("500"))
    await a.place_order(
        OrderRequest(client_order_id="b", symbol="X", side="BUY", type="MARKET", qty=Decimal("1"))
    )
    await a.place_order(
        OrderRequest(client_order_id="s", symbol="X", side="SELL", type="MARKET", qty=Decimal("1"))
    )
    assert await a.get_positions() == []
    with pytest.raises(RejectedError, match="insufficient funds"):
        await a.place_order(
            OrderRequest(
                client_order_id="big", symbol="X", side="BUY", type="MARKET", qty=Decimal("100")
            )
        )


async def test_cancel_unknown_order_rejected() -> None:
    with pytest.raises(RejectedError):
        await _adapter().cancel_order("nope", "X")
