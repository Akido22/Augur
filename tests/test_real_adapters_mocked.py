"""Реальные адаптеры с подменёнными SDK: ключи не нужны, сеть не используется."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from augur.adapters import (
    AdapterNotConnectedError,
    CallPolicy,
    FatalAdapterError,
    MarketAdapter,
    MarketStatus,
    OrderRequest,
    RejectedError,
    RetryableError,
    call_with_retry,
)
from augur.adapters.alpaca import AlpacaAdapter, classify_alpaca_error
from augur.adapters.ccxt_adapter import CcxtAdapter, classify_ccxt_error
from augur.adapters.mt5 import Mt5Adapter
from augur.data.models import timeframe_ms
from tests.conftest import make_settings

H = timeframe_ms("1h")
T0 = 1_700_000_000_000 // H * H
FAST = CallPolicy(timeout_s=2, max_retries=2, backoff_base_s=0)
ORDER = OrderRequest(client_order_id="c", symbol="X", side="BUY", type="MARKET", qty=Decimal(1))


# --- retry ------------------------------------------------------------------------


async def _no_sleep(_: float) -> None:
    return None


async def test_retry_recovers_after_retryable_errors() -> None:
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RetryableError("boom")
        return "ok"

    assert await call_with_retry(flaky, FAST, sleep=_no_sleep) == "ok"
    assert calls == 3


async def test_retry_gives_up_and_does_not_retry_rejected() -> None:
    calls = 0

    async def always() -> None:
        nonlocal calls
        calls += 1
        raise RetryableError("down")

    with pytest.raises(RetryableError, match="exhausted"):
        await call_with_retry(always, FAST, sleep=_no_sleep)
    assert calls == 3

    async def rejected() -> None:
        nonlocal calls
        calls += 1
        raise RejectedError("bad symbol")

    calls = 0
    with pytest.raises(RejectedError):
        await call_with_retry(rejected, FAST, sleep=_no_sleep)
    assert calls == 1


# --- CCXT -------------------------------------------------------------------------


class NetworkError(Exception): ...


class RequestTimeout(NetworkError): ...


class AuthenticationError(Exception): ...


class BadSymbol(Exception): ...


class FakeExchange:
    def __init__(self, config: dict[str, Any], rows: list[list[float]]) -> None:
        self.config = config
        self.rows = rows
        self.ohlcv_calls: list[tuple[str, str, int, int]] = []
        self.fail_first_ohlcv = False
        self.closed = False

    async def load_markets(self) -> dict[str, Any]:
        return {}

    async def fetch_ohlcv(self, symbol: str, tf: str, since: int, limit: int) -> list[list[float]]:
        self.ohlcv_calls.append((symbol, tf, since, limit))
        if self.fail_first_ohlcv:
            self.fail_first_ohlcv = False
            raise RequestTimeout("slow")
        return [r for r in self.rows if r[0] >= since][:limit]

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return {"bid": 99.5, "ask": 100.5, "last": 100.0, "timestamp": T0, "baseVolume": 12.0}

    async def fetch_balance(self) -> dict[str, Any]:
        return {
            "total": {"USDT": 1500.0, "BTC": 0.5, "ETH": 0},
            "free": {"USDT": 1000.0},
            "used": {"USDT": 500.0},
        }

    def market(self, symbol: str) -> dict[str, Any]:
        if symbol != "BTC/USDT":
            raise BadSymbol(symbol)
        return {
            "precision": {"amount": 0.0001, "price": 0.01},
            "limits": {"amount": {"min": 0.0001}, "cost": {"min": 5}},
            "active": True,
        }

    async def close(self) -> None:
        self.closed = True


def _rows(n: int) -> list[list[float]]:
    return [[T0 + i * H, 100 + i, 101 + i, 99 + i, 100.5 + i, 10.0] for i in range(n)]


async def _ccxt(rows: list[list[float]], **settings: Any) -> tuple[CcxtAdapter, FakeExchange]:
    holder: dict[str, FakeExchange] = {}

    def factory(config: dict[str, Any]) -> FakeExchange:
        holder["ex"] = FakeExchange(config, rows)
        return holder["ex"]

    adapter = CcxtAdapter(
        make_settings(**settings), policy=FAST, exchange_factory=factory, page_limit=10
    )
    await adapter.connect()
    return adapter, holder["ex"]


async def test_ccxt_paginates_and_filters_range() -> None:
    adapter, ex = await _ccxt(_rows(25))
    bars = await adapter.get_historical("BTC/USDT", "1h", T0, T0 + 23 * H)
    assert [b.ts_open for b in bars] == [T0 + i * H for i in range(23)]
    assert len(ex.ohlcv_calls) == 3  # страницы по 10
    assert bars[0].venue == "ccxt:binance" and bars[0].close == 100.5


async def test_ccxt_retries_timeout_then_succeeds() -> None:
    adapter, ex = await _ccxt(_rows(3))
    ex.fail_first_ohlcv = True
    bars = await adapter.get_historical("BTC/USDT", "1h", T0, T0 + 3 * H)
    assert len(bars) == 3


async def test_ccxt_passes_keys_from_settings_only() -> None:
    _, ex = await _ccxt([], ccxt_api_key=SecretStr("k-test"), ccxt_api_secret=SecretStr("s-test"))
    assert ex.config == {"enableRateLimit": True, "apiKey": "k-test", "secret": "s-test"}


async def test_ccxt_account_positions_ticker_instrument() -> None:
    adapter, _ = await _ccxt([])
    acct = await adapter.get_account_state()
    assert (acct.equity, acct.cash, acct.margin_used, acct.currency) == (
        Decimal("1500.0"),
        Decimal("1000.0"),
        Decimal("500.0"),
        "USDT",
    )
    [pos] = await adapter.get_positions()
    assert (pos.symbol, pos.qty) == ("BTC/USDT", Decimal("0.5"))
    tick = await adapter.get_latest("BTC/USDT")
    assert (tick.bid, tick.ask, tick.ts) == (Decimal("99.5"), Decimal("100.5"), T0)
    spec = await adapter.get_instrument("BTC/USDT")
    assert (spec.qty_step, spec.min_notional, spec.price_tick) == (
        Decimal("0.0001"),
        Decimal("5"),
        Decimal("0.01"),
    )
    with pytest.raises(RejectedError):
        await adapter.get_instrument("NOPE/USDT")
    assert await adapter.market_status("BTC/USDT") is MarketStatus.OPEN


def test_ccxt_error_classification() -> None:
    assert isinstance(classify_ccxt_error(RequestTimeout("t")), RetryableError)
    assert isinstance(classify_ccxt_error(AuthenticationError("a")), FatalAdapterError)
    assert isinstance(classify_ccxt_error(BadSymbol("b")), RejectedError)


async def test_ccxt_requires_connect_and_close() -> None:
    adapter = CcxtAdapter(make_settings(), policy=FAST)
    with pytest.raises(AdapterNotConnectedError):
        await adapter.get_latest("BTC/USDT")
    adapter, ex = await _ccxt([])
    await adapter.close()
    assert ex.closed


# --- Alpaca -----------------------------------------------------------------------


class APIError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"http {status_code}")
        self.status_code = status_code


def _alpaca(data: Any, trading: Any) -> AlpacaAdapter:
    adapter = AlpacaAdapter(make_settings(), policy=FAST, trading_client=trading, data_client=data)
    adapter._bars_request = lambda *a: ("bars", a)  # type: ignore[method-assign]
    adapter._quote_request = lambda s: ("quote", s)  # type: ignore[method-assign]
    return adapter


async def test_alpaca_bars_account_positions_clock() -> None:
    ts = [datetime.fromtimestamp((T0 + i * H) / 1000, tz=UTC) for i in range(3)]
    bars = [
        SimpleNamespace(timestamp=t, open=1, high=2, low=0.5, close=1.5, volume=100) for t in ts
    ]
    data = SimpleNamespace(
        get_stock_bars=lambda req: SimpleNamespace(data={"AAPL": bars}),
        get_stock_latest_quote=lambda req: {
            "AAPL": SimpleNamespace(timestamp=ts[0], bid_price=189.9, ask_price=190.1)
        },
    )
    trading = SimpleNamespace(
        get_account=lambda: SimpleNamespace(
            equity="1000", cash="400", initial_margin="0", currency="USD"
        ),
        get_all_positions=lambda: [
            SimpleNamespace(symbol="AAPL", qty="3", avg_entry_price="180", side="long"),
            SimpleNamespace(
                symbol="TSLA", qty="2", avg_entry_price="250", side="PositionSide.SHORT"
            ),
        ],
        get_clock=lambda: SimpleNamespace(is_open=False),
    )
    a = _alpaca(data, trading)
    await a.connect()  # клиенты переданы — SDK не нужен
    got = await a.get_historical("AAPL", "1h", T0, T0 + 3 * H)
    assert [b.ts_open for b in got] == [T0, T0 + H, T0 + 2 * H]
    tick = await a.get_latest("AAPL")
    assert (tick.bid, tick.ask, tick.ts) == (Decimal("189.9"), Decimal("190.1"), T0)
    acct = await a.get_account_state()
    assert (acct.equity, acct.cash, acct.currency) == (Decimal("1000"), Decimal("400"), "USD")
    pos = await a.get_positions()
    assert [(p.symbol, p.qty) for p in pos] == [("AAPL", Decimal(3)), ("TSLA", Decimal(-2))]
    assert await a.market_status("AAPL") is MarketStatus.CLOSED


async def test_alpaca_auth_error_is_fatal_and_not_retried() -> None:
    calls = 0

    def get_account() -> None:
        nonlocal calls
        calls += 1
        raise APIError(401)

    a = _alpaca(SimpleNamespace(), SimpleNamespace(get_account=get_account))
    with pytest.raises(FatalAdapterError):
        await a.get_account_state()
    assert calls == 1


def test_alpaca_error_classification() -> None:
    assert isinstance(classify_alpaca_error(APIError(429)), RetryableError)
    assert isinstance(classify_alpaca_error(APIError(503)), RetryableError)
    assert isinstance(classify_alpaca_error(APIError(422)), RejectedError)


async def test_alpaca_connect_without_keys_is_fatal() -> None:
    with pytest.raises(FatalAdapterError, match="ALPACA"):
        await AlpacaAdapter(make_settings()).connect()


# --- MT5 --------------------------------------------------------------------------


class FakeMt5:
    TIMEFRAME_H1 = 16385

    def __init__(self, init_ok: bool = True) -> None:
        self.init_ok = init_ok
        self.init_kwargs: dict[str, Any] = {}
        self.range_args: tuple[Any, ...] = ()
        self.shut = False

    def initialize(self, **kw: Any) -> bool:
        self.init_kwargs = kw
        return self.init_ok

    def last_error(self) -> tuple[int, str]:
        return (-6, "Terminal: Authorization failed")

    def shutdown(self) -> None:
        self.shut = True

    def copy_rates_range(self, symbol: str, tf: int, date_from: datetime, date_to: datetime) -> Any:
        self.range_args = (symbol, tf, date_from, date_to)
        start_s = int(date_from.timestamp())
        return [
            {
                "time": start_s + i * 3600,
                "open": 1.1,
                "high": 1.2,
                "low": 1.0,
                "close": 1.15,
                "tick_volume": 50,
                "real_volume": 0,
            }
            for i in range(3)
        ]

    def symbol_info_tick(self, symbol: str) -> Any:
        return SimpleNamespace(time_msc=T0 + 2 * H, bid=1.1, ask=1.1002, last=0.0)

    def positions_get(self) -> Any:
        return [
            SimpleNamespace(symbol="EURUSD", volume=0.1, type=0, price_open=1.09),
            SimpleNamespace(symbol="GBPUSD", volume=0.2, type=1, price_open=1.25),
        ]

    def account_info(self) -> Any:
        return SimpleNamespace(equity=1010.5, balance=1000.0, margin=20.0, currency="USD")

    def symbol_info(self, symbol: str) -> Any:
        if symbol != "EURUSD":
            return None
        return SimpleNamespace(
            volume_step=0.01,
            volume_min=0.01,
            trade_tick_size=0.00001,
            trade_contract_size=100000,
            trade_mode=4,
        )


def _mt5_settings() -> Any:
    return make_settings(
        mt5_login=123, mt5_password=SecretStr("p-test"), mt5_server="Demo", mt5_server_utc_offset=2
    )


async def test_mt5_reads_and_converts_server_time_to_utc() -> None:
    fake = FakeMt5()
    a = Mt5Adapter(_mt5_settings(), policy=FAST, mt5_module=fake)
    await a.connect()
    assert fake.init_kwargs == {"login": 123, "password": "p-test", "server": "Demo"}
    bars = await a.get_historical("EURUSD", "1h", T0, T0 + 3 * H)
    # запрос уходит во времени сервера (+2 ч), ответ переводится обратно в UTC
    assert fake.range_args[2] == datetime.fromtimestamp((T0 + 2 * H) / 1000, tz=UTC)
    assert [b.ts_open for b in bars] == [T0, T0 + H, T0 + 2 * H]
    tick = await a.get_latest("EURUSD")
    assert tick.ts == T0 and tick.last is None
    pos = await a.get_positions()
    assert [(p.symbol, p.qty) for p in pos] == [
        ("EURUSD", Decimal("0.1")),
        ("GBPUSD", Decimal("-0.2")),
    ]
    acct = await a.get_account_state()
    assert (acct.equity, acct.cash, acct.margin_used) == (
        Decimal("1010.5"),
        Decimal("1000.0"),
        Decimal("20.0"),
    )
    spec = await a.get_instrument("EURUSD")
    assert spec.contract_size == Decimal(100000)
    with pytest.raises(RejectedError):
        await a.get_instrument("XXX")
    assert await a.market_status("EURUSD") is MarketStatus.OPEN
    await a.close()
    assert fake.shut


async def test_mt5_initialize_failure_is_fatal() -> None:
    with pytest.raises(FatalAdapterError, match="Authorization failed"):
        await Mt5Adapter(_mt5_settings(), policy=FAST, mt5_module=FakeMt5(init_ok=False)).connect()


async def test_mt5_connect_without_credentials_is_fatal() -> None:
    with pytest.raises(FatalAdapterError, match="MT5_LOGIN"):
        await Mt5Adapter(make_settings(), mt5_module=FakeMt5()).connect()


# --- общее ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter",
    [
        CcxtAdapter(make_settings()),
        AlpacaAdapter(make_settings()),
        Mt5Adapter(make_settings(), mt5_module=FakeMt5()),
    ],
    ids=["ccxt", "alpaca", "mt5"],
)
async def test_real_adapters_are_read_only(adapter: MarketAdapter) -> None:
    assert isinstance(adapter, MarketAdapter)
    for call in (
        adapter.place_order(ORDER),
        adapter.cancel_order("c", "X"),
        adapter.get_order("c", "X"),
    ):
        with pytest.raises(NotImplementedError, match="0.7.0"):
            await call
