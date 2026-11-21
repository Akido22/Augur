"""CCXT-адаптер криптобирж — только чтение (ARCHITECTURE.md §4.4).

Ордера (`place_order`, `cancel_order`, `get_order`) — с 0.7.0.
Ключи берутся только из `Settings` (`SecretStr`) и нигде не логируются.
"""

import time
from collections.abc import Awaitable, Callable
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

_RETRYABLE = {
    "NetworkError",
    "RequestTimeout",
    "RateLimitExceeded",
    "ExchangeNotAvailable",
    "DDoSProtection",
    "OnMaintenance",
}
_FATAL = {"AuthenticationError", "PermissionDenied", "AccountSuspended"}
_REJECTED = {"InsufficientFunds", "InvalidOrder", "BadSymbol", "BadRequest", "OrderNotFound"}


def classify_ccxt_error(e: BaseException) -> AdapterError:
    """Отображает исключение ccxt на иерархию ошибок адаптера (ARCHITECTURE.md §4.2)."""
    names = exc_names(e)
    msg = f"{type(e).__name__}: {e}"
    if names & _FATAL:
        return FatalAdapterError(msg)
    if names & _RETRYABLE:
        return RetryableError(msg)
    if names & _REJECTED:
        return RejectedError(msg)
    return AdapterError(msg)


ExchangeFactory = Callable[[dict[str, Any]], Any]


class CcxtAdapter:
    """Адаптер к бирже через `ccxt.async_support`."""

    def __init__(
        self,
        settings: Settings,
        *,
        quote_currency: str = "USDT",
        policy: CallPolicy | None = None,
        exchange_factory: ExchangeFactory | None = None,
        page_limit: int = 1000,
    ) -> None:
        """
        Args:
            settings: настройки; используются `ccxt_exchange`, `ccxt_api_key`, `ccxt_api_secret`.
            quote_currency: валюта, в которой считается equity (без конвертации прочих активов).
            policy: таймауты и повторы.
            exchange_factory: фабрика объекта биржи (подменяется в тестах).
            page_limit: размер страницы `fetch_ohlcv`.
        """
        self.venue = f"ccxt:{settings.ccxt_exchange}"
        self._settings = settings
        self._quote = quote_currency
        self._policy = policy or CallPolicy()
        self._factory = exchange_factory
        self._page_limit = page_limit
        self._ex: Any = None

    async def connect(self) -> None:
        config: dict[str, Any] = {"enableRateLimit": True}
        if self._settings.ccxt_api_key is not None:
            config["apiKey"] = self._settings.ccxt_api_key.get_secret_value()
        if self._settings.ccxt_api_secret is not None:
            config["secret"] = self._settings.ccxt_api_secret.get_secret_value()
        if self._factory is not None:
            self._ex = self._factory(config)
        else:
            ccxt = import_sdk("ccxt.async_support", "ccxt")
            exchange_cls = getattr(ccxt, self._settings.ccxt_exchange, None)
            if exchange_cls is None:
                raise FatalAdapterError(f"unknown ccxt exchange: {self._settings.ccxt_exchange}")
            self._ex = exchange_cls(config)
        await self._call(lambda: self._ex.load_markets())

    async def close(self) -> None:
        if self._ex is not None:
            await self._ex.close()
            self._ex = None

    async def _call(self, fn: Callable[[], Awaitable[T]]) -> T:
        if self._ex is None:
            raise AdapterNotConnectedError("call connect() first")

        async def wrapped() -> T:
            try:
                return await fn()
            except AdapterError:
                raise
            except Exception as e:  # noqa: BLE001 — SDK бросает собственные классы
                raise classify_ccxt_error(e) from e

        return await call_with_retry(wrapped, self._policy)

    async def get_historical(self, symbol: str, timeframe: str, start: int, end: int) -> list[Bar]:
        """`fetch_ohlcv` с пагинацией по `since`."""
        tf = timeframe_ms(timeframe)
        now = int(time.time() * 1000)
        bars: dict[int, Bar] = {}
        since = start
        while since < end:
            rows: list[list[Any]] = await self._call(
                lambda cursor=since: self._ex.fetch_ohlcv(  # type: ignore[misc]
                    symbol, timeframe, cursor, self._page_limit
                )
            )
            if not rows:
                break
            for ts, o, h, low, c, v in rows:
                ts = int(ts)
                if start <= ts < end:
                    bars[ts] = Bar(
                        symbol=symbol,
                        venue=self.venue,
                        timeframe=timeframe,
                        ts_open=ts,
                        open=float(o),
                        high=float(h),
                        low=float(low),
                        close=float(c),
                        volume=float(v or 0),
                        is_closed=ts + tf <= now,
                    )
            last_ts = int(rows[-1][0])
            if last_ts < since:  # защита от зацикливания на кривом ответе биржи
                break
            since = last_ts + tf
        return [bars[k] for k in sorted(bars)]

    async def get_latest(self, symbol: str) -> Tick:
        t: dict[str, Any] = await self._call(lambda: self._ex.fetch_ticker(symbol))

        def dec(x: Any) -> Decimal | None:
            return None if x is None else Decimal(str(x))

        last = dec(t.get("last"))
        bid = dec(t.get("bid")) or last
        ask = dec(t.get("ask")) or last
        if bid is None or ask is None:
            raise RejectedError(f"no quote for {symbol}")
        ts = t.get("timestamp") or int(time.time() * 1000)
        vol = t.get("baseVolume")
        return Tick(
            symbol=symbol,
            venue=self.venue,
            ts=int(ts),
            bid=bid,
            ask=ask,
            last=last,
            volume=None if vol is None else float(vol),
        )

    async def get_positions(self) -> list[VenuePosition]:
        """Spot: ненулевые балансы (кроме валюты котировки) как позиции без средней цены."""
        bal: dict[str, Any] = await self._call(lambda: self._ex.fetch_balance())
        totals: dict[str, Any] = bal.get("total") or {}
        return [
            VenuePosition(symbol=f"{ccy}/{self._quote}", qty=Decimal(str(amount)))
            for ccy, amount in sorted(totals.items())
            if amount and ccy != self._quote
        ]

    async def get_account_state(self) -> AccountState:
        bal: dict[str, Any] = await self._call(lambda: self._ex.fetch_balance())
        total = Decimal(str((bal.get("total") or {}).get(self._quote) or 0))
        free = Decimal(str((bal.get("free") or {}).get(self._quote) or 0))
        used = Decimal(str((bal.get("used") or {}).get(self._quote) or 0))
        return AccountState(
            equity=total,
            cash=free,
            margin_used=used,
            currency=self._quote,
            ts=int(time.time() * 1000),
        )

    async def get_instrument(self, symbol: str) -> InstrumentSpec:
        if self._ex is None:
            raise AdapterNotConnectedError("call connect() first")
        try:
            m: dict[str, Any] = self._ex.market(symbol)
        except Exception as e:  # noqa: BLE001
            raise classify_ccxt_error(e) from e
        precision = m.get("precision") or {}
        limits = m.get("limits") or {}

        def step(p: Any) -> Decimal:
            # ccxt ≥4 по умолчанию отдаёт precision как шаг (TICK_SIZE)
            return Decimal(str(p)) if p is not None else Decimal("0.00000001")

        return InstrumentSpec(
            symbol=symbol,
            qty_step=step(precision.get("amount")),
            min_qty=Decimal(str((limits.get("amount") or {}).get("min") or 0)),
            min_notional=Decimal(str((limits.get("cost") or {}).get("min") or 0)),
            price_tick=step(precision.get("price")),
            contract_size=Decimal(str(m.get("contractSize") or 1)),
        )

    async def market_status(self, symbol: str) -> MarketStatus:
        if self._ex is None:
            raise AdapterNotConnectedError("call connect() first")
        m: dict[str, Any] = self._ex.market(symbol)
        return MarketStatus.OPEN if m.get("active", True) else MarketStatus.HALTED

    async def place_order(self, req: OrderRequest) -> OrderAck:
        raise NotImplementedError(ORDERS_NOT_SUPPORTED)

    async def cancel_order(self, client_order_id: str, symbol: str) -> OrderAck:
        raise NotImplementedError(ORDERS_NOT_SUPPORTED)

    async def get_order(self, client_order_id: str, symbol: str) -> OrderAck | None:
        raise NotImplementedError(ORDERS_NOT_SUPPORTED)
