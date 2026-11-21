"""Alpaca-адаптер акций — только чтение (ARCHITECTURE.md §4.5), библиотека `alpaca-py`.

SDK синхронный, вызовы уходят в `asyncio.to_thread`. Ордера — с 0.7.0.
"""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, TypeVar

from augur.adapters._sdk import exc_names, import_sdk
from augur.adapters.base import (
    ORDERS_NOT_SUPPORTED,
    AccountState,
    AdapterError,
    AdapterNotConnectedError,
    FatalAdapterError,
    InstrumentSpec,
    MarketStatus,
    OrderAck,
    OrderRequest,
    RejectedError,
    RetryableError,
    VenuePosition,
)
from augur.adapters.resilient import CallPolicy, call_with_retry
from augur.config.settings import Settings
from augur.data.models import Bar, Tick, timeframe_ms

T = TypeVar("T")

_NETWORK = {"ConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout", "TimeoutError"}


def classify_alpaca_error(e: BaseException) -> AdapterError:
    """HTTP-статус APIError → класс ошибки; сетевые исключения → RetryableError."""
    msg = f"{type(e).__name__}: {e}"
    status = getattr(e, "status_code", None)
    if isinstance(status, int):
        if status in (401, 403):
            return FatalAdapterError(msg)
        if status == 429 or status >= 500:
            return RetryableError(msg)
        return RejectedError(msg)
    if exc_names(e) & _NETWORK:
        return RetryableError(msg)
    return AdapterError(msg)


def _ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


class AlpacaAdapter:
    """Адаптер к Alpaca: `TradingClient` (счёт, позиции, часы) + `StockHistoricalDataClient`."""

    def __init__(
        self,
        settings: Settings,
        *,
        policy: CallPolicy | None = None,
        trading_client: Any = None,
        data_client: Any = None,
    ) -> None:
        """
        Args:
            settings: `alpaca_api_key`, `alpaca_api_secret`, `alpaca_paper`.
            policy: таймауты и повторы.
            trading_client, data_client: готовые клиенты (подменяются в тестах).
        """
        self.venue = "alpaca"
        self._settings = settings
        self._policy = policy or CallPolicy()
        self._trading = trading_client
        self._data = data_client

    async def connect(self) -> None:
        if self._trading is not None and self._data is not None:
            return
        key, secret = self._settings.alpaca_api_key, self._settings.alpaca_api_secret
        if key is None or secret is None:
            raise FatalAdapterError("ALPACA_API_KEY / ALPACA_API_SECRET не заданы")
        trading_mod = import_sdk("alpaca.trading.client", "alpaca")
        data_mod = import_sdk("alpaca.data.historical", "alpaca")
        self._trading = trading_mod.TradingClient(
            key.get_secret_value(), secret.get_secret_value(), paper=self._settings.alpaca_paper
        )
        self._data = data_mod.StockHistoricalDataClient(
            key.get_secret_value(), secret.get_secret_value()
        )

    async def close(self) -> None:
        self._trading = None
        self._data = None

    async def _call(self, fn: Callable[[], T]) -> T:
        if self._trading is None or self._data is None:
            raise AdapterNotConnectedError("call connect() first")

        async def wrapped() -> T:
            try:
                return await asyncio.to_thread(fn)
            except Exception as e:  # noqa: BLE001 — SDK бросает собственные классы
                raise classify_alpaca_error(e) from e

        return await call_with_retry(wrapped, self._policy)

    # --- построение запросов (вынесено, чтобы тесты работали без alpaca-py) ---

    def _bars_request(self, symbol: str, timeframe: str, start: int, end: int) -> Any:
        req = import_sdk("alpaca.data.requests", "alpaca")
        tfm = import_sdk("alpaca.data.timeframe", "alpaca")
        unit, amount = {
            "1m": (tfm.TimeFrameUnit.Minute, 1),
            "5m": (tfm.TimeFrameUnit.Minute, 5),
            "15m": (tfm.TimeFrameUnit.Minute, 15),
            "1h": (tfm.TimeFrameUnit.Hour, 1),
            "4h": (tfm.TimeFrameUnit.Hour, 4),
            "1d": (tfm.TimeFrameUnit.Day, 1),
        }[timeframe]
        return req.StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tfm.TimeFrame(amount, unit),
            start=_dt(start),
            end=_dt(end),
        )

    def _quote_request(self, symbol: str) -> Any:
        req = import_sdk("alpaca.data.requests", "alpaca")
        return req.StockLatestQuoteRequest(symbol_or_symbols=symbol)

    # --- данные ---------------------------------------------------------------

    async def get_historical(self, symbol: str, timeframe: str, start: int, end: int) -> list[Bar]:
        tf = timeframe_ms(timeframe)
        request = self._bars_request(symbol, timeframe, start, end)
        result: Any = await self._call(lambda: self._data.get_stock_bars(request))
        data = getattr(result, "data", result)
        rows = data.get(symbol, []) if isinstance(data, dict) else []
        now = int(time.time() * 1000)
        bars = {}
        for b in rows:
            ts = _ms(b.timestamp)
            if start <= ts < end:
                bars[ts] = Bar(
                    symbol=symbol,
                    venue=self.venue,
                    timeframe=timeframe,
                    ts_open=ts,
                    open=float(b.open),
                    high=float(b.high),
                    low=float(b.low),
                    close=float(b.close),
                    volume=float(b.volume),
                    is_closed=ts + tf <= now,
                )
        return [bars[k] for k in sorted(bars)]

    async def get_latest(self, symbol: str) -> Tick:
        request = self._quote_request(symbol)
        result: Any = await self._call(lambda: self._data.get_stock_latest_quote(request))
        q = result[symbol]
        return Tick(
            symbol=symbol,
            venue=self.venue,
            ts=_ms(q.timestamp),
            bid=Decimal(str(q.bid_price)),
            ask=Decimal(str(q.ask_price)),
        )

    async def get_positions(self) -> list[VenuePosition]:
        positions: list[Any] = await self._call(lambda: self._trading.get_all_positions())
        out = []
        for p in positions:
            qty = Decimal(str(p.qty))
            side = str(getattr(p, "side", "long")).lower()
            if side.endswith("short") and qty > 0:
                qty = -qty
            out.append(
                VenuePosition(symbol=p.symbol, qty=qty, avg_price=Decimal(str(p.avg_entry_price)))
            )
        return out

    async def get_account_state(self) -> AccountState:
        a: Any = await self._call(lambda: self._trading.get_account())
        return AccountState(
            equity=Decimal(str(a.equity)),
            cash=Decimal(str(a.cash)),
            margin_used=Decimal(str(getattr(a, "initial_margin", 0) or 0)),
            currency=str(getattr(a, "currency", "USD") or "USD"),
            ts=int(time.time() * 1000),
        )

    async def get_instrument(self, symbol: str) -> InstrumentSpec:
        """Американские акции: шаг цены 0.01, целые лоты (дробные — не используются в MVP)."""
        return InstrumentSpec(
            symbol=symbol,
            qty_step=Decimal(1),
            min_qty=Decimal(1),
            min_notional=Decimal(1),
            price_tick=Decimal("0.01"),
            trading_hours="NYSE regular session",
        )

    async def market_status(self, symbol: str) -> MarketStatus:
        clock: Any = await self._call(lambda: self._trading.get_clock())
        return MarketStatus.OPEN if clock.is_open else MarketStatus.CLOSED

    async def place_order(self, req: OrderRequest) -> OrderAck:
        raise NotImplementedError(ORDERS_NOT_SUPPORTED)

    async def cancel_order(self, client_order_id: str, symbol: str) -> OrderAck:
        raise NotImplementedError(ORDERS_NOT_SUPPORTED)

    async def get_order(self, client_order_id: str, symbol: str) -> OrderAck | None:
        raise NotImplementedError(ORDERS_NOT_SUPPORTED)
