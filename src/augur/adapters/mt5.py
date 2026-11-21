"""MetaTrader 5 — Форекс, только чтение (ARCHITECTURE.md §4.3).

Пакет `MetaTrader5` работает только на Windows с запущенным терминалом. API
синхронный и не потокобезопасный: все вызовы идут в одном выделенном потоке.
Время MT5 — время сервера брокера; перевод в UTC по `settings.mt5_server_utc_offset`
(часы) `[ДОПУЩЕНИЕ ARCH §4.3]`. Ордера — с 0.7.0.
"""

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, TypeVar

from augur.adapters._sdk import import_sdk
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

_TIMEFRAMES = {
    "1m": "TIMEFRAME_M1",
    "5m": "TIMEFRAME_M5",
    "15m": "TIMEFRAME_M15",
    "1h": "TIMEFRAME_H1",
    "4h": "TIMEFRAME_H4",
    "1d": "TIMEFRAME_D1",
}
_POSITION_TYPE_SELL = 1  # mt5.POSITION_TYPE_SELL
_SYMBOL_TRADE_MODE_DISABLED = 0  # mt5.SYMBOL_TRADE_MODE_DISABLED
# Коды last_error(): <0 — ошибки терминала/IPC. -10003/-10004/-10005 — нет связи/таймаут IPC.
_RETRYABLE_CODES = {-10003, -10004, -10005}


class Mt5Adapter:
    """Адаптер к терминалу MetaTrader 5."""

    def __init__(
        self,
        settings: Settings,
        *,
        policy: CallPolicy | None = None,
        mt5_module: Any = None,
    ) -> None:
        """
        Args:
            settings: `mt5_login`, `mt5_password`, `mt5_server`, `mt5_server_utc_offset`.
            policy: таймауты и повторы.
            mt5_module: модуль MetaTrader5 (подменяется в тестах).
        """
        self.venue = "mt5"
        self._settings = settings
        self._policy = policy or CallPolicy()
        self._mt5 = mt5_module
        self._offset_ms = settings.mt5_server_utc_offset * 3_600_000
        self._executor: ThreadPoolExecutor | None = None
        self._connected = False

    # --- инфраструктура -------------------------------------------------------

    def _error(self, what: str) -> AdapterError:
        code, msg = self._mt5.last_error()
        text = f"{what} failed: [{code}] {msg}"
        if code in _RETRYABLE_CODES:
            return RetryableError(text)
        return AdapterError(text)

    async def _run(self, fn: Callable[[], T]) -> T:
        if self._executor is None:
            raise AdapterNotConnectedError("call connect() first")
        loop = asyncio.get_running_loop()
        executor = self._executor

        async def once() -> T:
            return await loop.run_in_executor(executor, fn)

        return await call_with_retry(once, self._policy)

    async def connect(self) -> None:
        if self._mt5 is None:
            self._mt5 = import_sdk("MetaTrader5", "mt5")
        s = self._settings
        if s.mt5_login is None or s.mt5_password is None or s.mt5_server is None:
            raise FatalAdapterError("MT5_LOGIN / MT5_PASSWORD / MT5_SERVER не заданы")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")
        login, password, server = s.mt5_login, s.mt5_password.get_secret_value(), s.mt5_server

        def init() -> bool:
            return bool(self._mt5.initialize(login=login, password=password, server=server))

        ok = await self._run(init)
        if not ok:
            code, msg = self._mt5.last_error()
            await self.close()
            raise FatalAdapterError(f"mt5.initialize failed: [{code}] {msg}")
        self._connected = True

    async def close(self) -> None:
        if self._executor is not None:
            if self._connected:
                await asyncio.get_running_loop().run_in_executor(self._executor, self._mt5.shutdown)
            self._executor.shutdown(wait=True)
        self._executor = None
        self._connected = False

    def _to_utc_ms(self, server_seconds: float) -> int:
        return int(server_seconds * 1000) - self._offset_ms

    def _to_server_dt(self, utc_ms: int) -> datetime:
        return datetime.fromtimestamp((utc_ms + self._offset_ms) / 1000, tz=UTC)

    # --- данные ---------------------------------------------------------------

    async def get_historical(self, symbol: str, timeframe: str, start: int, end: int) -> list[Bar]:
        tf = timeframe_ms(timeframe)
        mt5_tf = getattr(self._mt5, _TIMEFRAMES[timeframe])
        date_from, date_to = self._to_server_dt(start), self._to_server_dt(end)

        def fetch() -> Any:
            rates = self._mt5.copy_rates_range(symbol, mt5_tf, date_from, date_to)
            if rates is None:
                raise self._error("copy_rates_range")
            return rates

        rates = await self._run(fetch)
        now = int(time.time() * 1000)
        bars: dict[int, Bar] = {}
        for r in rates:
            ts = self._to_utc_ms(float(r["time"]))
            if start <= ts < end:
                bars[ts] = Bar(
                    symbol=symbol,
                    venue=self.venue,
                    timeframe=timeframe,
                    ts_open=ts,
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r["tick_volume"]),
                    is_closed=ts + tf <= now,
                )
        return [bars[k] for k in sorted(bars)]

    async def get_latest(self, symbol: str) -> Tick:
        def fetch() -> Any:
            t = self._mt5.symbol_info_tick(symbol)
            if t is None:
                raise self._error("symbol_info_tick")
            return t

        t = await self._run(fetch)
        return Tick(
            symbol=symbol,
            venue=self.venue,
            ts=int(t.time_msc) - self._offset_ms,
            bid=Decimal(str(t.bid)),
            ask=Decimal(str(t.ask)),
            last=Decimal(str(t.last)) if t.last else None,
        )

    async def get_positions(self) -> list[VenuePosition]:
        def fetch() -> Any:
            p = self._mt5.positions_get()
            if p is None:
                raise self._error("positions_get")
            return p

        out = []
        for p in await self._run(fetch):
            qty = Decimal(str(p.volume))
            if p.type == _POSITION_TYPE_SELL:
                qty = -qty
            out.append(
                VenuePosition(symbol=p.symbol, qty=qty, avg_price=Decimal(str(p.price_open)))
            )
        return out

    async def get_account_state(self) -> AccountState:
        def fetch() -> Any:
            a = self._mt5.account_info()
            if a is None:
                raise self._error("account_info")
            return a

        a = await self._run(fetch)
        return AccountState(
            equity=Decimal(str(a.equity)),
            cash=Decimal(str(a.balance)),
            margin_used=Decimal(str(a.margin)),
            currency=str(a.currency),
            ts=int(time.time() * 1000),
        )

    async def _symbol_info(self, symbol: str) -> Any:
        def fetch() -> Any:
            info = self._mt5.symbol_info(symbol)
            if info is None:
                raise RejectedError(f"unknown symbol: {symbol}")
            return info

        return await self._run(fetch)

    async def get_instrument(self, symbol: str) -> InstrumentSpec:
        i = await self._symbol_info(symbol)
        return InstrumentSpec(
            symbol=symbol,
            qty_step=Decimal(str(i.volume_step)),
            min_qty=Decimal(str(i.volume_min)),
            min_notional=Decimal(0),
            price_tick=Decimal(str(i.trade_tick_size)),
            contract_size=Decimal(str(i.trade_contract_size)),
        )

    async def market_status(self, symbol: str) -> MarketStatus:
        i = await self._symbol_info(symbol)
        return (
            MarketStatus.HALTED
            if i.trade_mode == _SYMBOL_TRADE_MODE_DISABLED
            else MarketStatus.OPEN
        )

    async def place_order(self, req: OrderRequest) -> OrderAck:
        raise NotImplementedError(ORDERS_NOT_SUPPORTED)

    async def cancel_order(self, client_order_id: str, symbol: str) -> OrderAck:
        raise NotImplementedError(ORDERS_NOT_SUPPORTED)

    async def get_order(self, client_order_id: str, symbol: str) -> OrderAck | None:
        raise NotImplementedError(ORDERS_NOT_SUPPORTED)
