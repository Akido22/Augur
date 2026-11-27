"""CcxtAdapter: ордера spot (0.7.0) на подменённой бирже — без сети и без ключей."""

from decimal import Decimal
from typing import Any

import pytest

from augur.adapters import CallPolicy, OrderRequest, OrderState, RejectedError, RetryableError
from augur.adapters.ccxt_adapter import CcxtAdapter
from tests.conftest import make_settings

FAST = CallPolicy(timeout_s=2, max_retries=2, backoff_base_s=0)


class RequestTimeout(Exception): ...


class InsufficientFunds(Exception): ...


class OrderExchange:
    """Минимальная spot-биржа: create/fetch/cancel ордеров по clientOrderId."""

    def __init__(self, config: dict[str, Any], *, fetch_orders: bool = True) -> None:
        self.config = config
        self.sandbox = False
        self.has = {"fetchOrders": fetch_orders}
        self.markets = {"BTC/USDT": {}}
        self.orders: list[dict[str, Any]] = []
        self.create_calls = 0
        self.create_error: Exception | None = None
        self.fill_on_create = True
        self.balance: dict[str, Any] = {"total": {"USDT": 80.0, "BTC": 0.2}, "free": {}, "used": {}}

    def set_sandbox_mode(self, enabled: bool) -> None:
        self.sandbox = enabled

    async def load_markets(self) -> dict[str, Any]:
        return self.markets

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{amount:.4f}"

    async def create_order(
        self, symbol: str, type_: str, side: str, amount: float, price: Any, params: Any
    ) -> dict[str, Any]:
        self.create_calls += 1
        if self.create_error is not None:
            raise self.create_error
        filled = amount if self.fill_on_create else 0.0
        row = {
            "id": f"v{len(self.orders) + 1}",
            "clientOrderId": params["clientOrderId"],
            "symbol": symbol,
            "type": type_,
            "side": side,
            "amount": amount,
            "filled": filled,
            "average": 100.0 if filled else None,
            "status": "closed" if filled else "open",
            "fee": {"cost": 0.0002 * filled / 0.2, "currency": "BTC"} if filled else None,
            "timestamp": 1,
        }
        self.orders.append(row)
        return dict(row)

    async def fetch_orders(self, symbol: str, since: Any, limit: int) -> list[dict[str, Any]]:
        return [dict(o) for o in self.orders if o["symbol"] == symbol]

    async def fetch_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return [dict(o) for o in self.orders if o["status"] == "open"]

    async def fetch_closed_orders(self, symbol: str, since: Any, limit: int) -> list[Any]:
        return [dict(o) for o in self.orders if o["status"] != "open"]

    async def cancel_order(self, venue_id: str, symbol: str) -> dict[str, Any]:
        for o in self.orders:
            if o["id"] == venue_id:
                o["status"] = "canceled"
                return dict(o)
        raise RuntimeError("unknown")

    async def fetch_balance(self) -> dict[str, Any]:
        return self.balance

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return {"last": 150.0, "bid": 149.0, "ask": 151.0}

    async def close(self) -> None:
        pass


async def _adapter(**kw: Any) -> tuple[CcxtAdapter, OrderExchange]:
    holder: dict[str, OrderExchange] = {}
    fetch_orders = kw.pop("fetch_orders", True)

    def factory(config: dict[str, Any]) -> OrderExchange:
        holder["ex"] = OrderExchange(config, fetch_orders=fetch_orders)
        return holder["ex"]

    a = CcxtAdapter(make_settings(**kw), policy=FAST, exchange_factory=factory)
    await a.connect()
    return a, holder["ex"]


def _req(coid: str = "jnABC", qty: str = "0.2", **kw: Any) -> OrderRequest:
    return OrderRequest(
        client_order_id=coid, symbol="BTC/USDT", side="BUY", type="MARKET", qty=Decimal(qty), **kw
    )


async def test_sandbox_by_default_and_explicit_real() -> None:
    a, ex = await _adapter()
    assert ex.sandbox and a.sandbox
    _, ex2 = await _adapter(ccxt_sandbox=False)
    assert not ex2.sandbox


async def test_place_order_passes_client_order_id_and_maps_fill() -> None:
    a, ex = await _adapter()
    ack = await a.place_order(_req())
    assert ex.orders[0]["clientOrderId"] == "jnABC"
    assert ack.state is OrderState.FILLED and ack.filled_qty == Decimal("0.2")
    assert ack.venue_order_id == "v1" and ack.avg_price == Decimal("100.0")
    # комиссия 0.0002 BTC → в USDT по средней цене
    assert ack.fee == Decimal("0.0002") * Decimal("100.0") and ack.fee_ccy == "USDT"


async def test_place_order_is_never_retried() -> None:
    """ARCH §4.2: таймаут place_order → одна попытка, решает OrderManager через get_order."""
    a, ex = await _adapter()
    ex.create_error = RequestTimeout("slow")
    with pytest.raises(RetryableError):
        await a.place_order(_req())
    assert ex.create_calls == 1


async def test_rejection_and_unsupported_types() -> None:
    a, ex = await _adapter()
    ex.create_error = InsufficientFunds("no money")
    with pytest.raises(RejectedError):
        await a.place_order(_req())
    with pytest.raises(RejectedError, match="not supported"):
        await a.place_order(
            OrderRequest(
                client_order_id="s",
                symbol="BTC/USDT",
                side="SELL",
                type="STOP",
                qty=Decimal("0.1"),
                stop_price=Decimal(90),
            )
        )


@pytest.mark.parametrize("fetch_orders", [True, False], ids=["fetchOrders", "open+closed"])
async def test_get_order_by_client_order_id(fetch_orders: bool) -> None:
    a, ex = await _adapter(fetch_orders=fetch_orders)
    assert await a.get_order("missing", "BTC/USDT") is None
    ex.fill_on_create = False
    await a.place_order(_req("jnOPEN"))
    ack = await a.get_order("jnOPEN", "BTC/USDT")
    assert ack is not None and ack.state is OrderState.SUBMITTED and ack.filled_qty == 0


async def test_cancel_is_idempotent_by_client_order_id() -> None:
    a, ex = await _adapter()
    ex.fill_on_create = False
    await a.place_order(_req("jnC"))
    ack = await a.cancel_order("jnC", "BTC/USDT")
    assert ack.state is OrderState.CANCELLED
    again = await a.cancel_order("jnC", "BTC/USDT")  # уже отменён — без повторной отмены
    assert again.state is OrderState.CANCELLED
    with pytest.raises(RejectedError, match="not found"):
        await a.cancel_order("nope", "BTC/USDT")


async def test_equity_marks_spot_holdings_to_market() -> None:
    """Без переоценки покупка BTC выглядела бы дневным убытком на весь notional."""
    a, _ = await _adapter()
    acct = await a.get_account_state()
    assert acct.equity == Decimal("80.0") + Decimal("0.2") * Decimal("150.0")
