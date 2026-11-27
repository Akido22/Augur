"""CCXT-адаптер криптобирж, spot (ARCHITECTURE.md §4.4).

Ордера (0.7.0, MICRO_LIVE):
- `place_order` — ОДНА попытка без повторов (ARCH §4.2): при таймауте Execution сначала
  спрашивает `get_order(client_order_id)`. `client_order_id` уходит на биржу как `clientOrderId`.
- `get_order` ищет ордер по `clientOrderId` среди последних ордеров символа (unified API ccxt);
  не найден → None.
- `cancel_order` идемпотентен: находит ордер по `clientOrderId`, отменяет по id биржи и
  возвращает итоговый статус.
- Spot: `reduce_only` и присоединённые SL/TP биржей не поддерживаются. Закрывающий объём
  ограничивает OrderManager (|позиция|), SL/TP — `OrderManager.check_protective_exits`.
- Комиссия в базовой валюте пересчитывается в валюту котировки по средней цене.

`ccxt_sandbox=true` (по умолчанию) включает testnet биржи. Ключи берутся только из `Settings`
(`SecretStr`) и нигде не логируются.
"""

import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, TypeVar

from augur.adapters._sdk import exc_names, import_sdk
from augur.adapters.base import (
    AccountState,
    AdapterError,
    AdapterNotConnectedError,
    FatalAdapterError,
    InstrumentSpec,
    MarketStatus,
    OrderAck,
    OrderRequest,
    OrderState,
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
        quote_currency: str | None = None,
        policy: CallPolicy | None = None,
        exchange_factory: ExchangeFactory | None = None,
        page_limit: int = 1000,
    ) -> None:
        """
        Args:
            settings: настройки; используются `ccxt_exchange`, `ccxt_api_key`, `ccxt_api_secret`.
            quote_currency: валюта equity (без конвертации прочих активов);
                None — `settings.ccxt_quote_currency`.
            policy: таймауты и повторы.
            exchange_factory: фабрика объекта биржи (подменяется в тестах).
            page_limit: размер страницы `fetch_ohlcv`.
        """
        self.venue = f"ccxt:{settings.ccxt_exchange}"
        self._settings = settings
        self._quote = quote_currency or settings.ccxt_quote_currency
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
        if self._settings.ccxt_sandbox:
            self._ex.set_sandbox_mode(True)
        await self._call(lambda: self._ex.load_markets())

    async def close(self) -> None:
        if self._ex is not None:
            await self._ex.close()
            self._ex = None

    @property
    def sandbox(self) -> bool:
        return self._settings.ccxt_sandbox

    async def _call(self, fn: Callable[[], Awaitable[T]], *, retry: bool = True) -> T:
        if self._ex is None:
            raise AdapterNotConnectedError("call connect() first")

        async def wrapped() -> T:
            try:
                return await fn()
            except AdapterError:
                raise
            except Exception as e:  # noqa: BLE001 — SDK бросает собственные классы
                raise classify_ccxt_error(e) from e

        policy = self._policy if retry else self._policy.model_copy(update={"max_retries": 0})
        return await call_with_retry(wrapped, policy)

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
        """Spot: equity = валюта котировки + базовые активы по последней цене (mark-to-market).

        Без переоценки покупка выглядела бы убытком на весь notional (R10 / CB daily_loss).
        Активы без рынка `<ccy>/<quote>` в equity не входят.
        """
        bal: dict[str, Any] = await self._call(lambda: self._ex.fetch_balance())
        totals: dict[str, Any] = bal.get("total") or {}
        total = Decimal(str(totals.get(self._quote) or 0))
        free = Decimal(str((bal.get("free") or {}).get(self._quote) or 0))
        used = Decimal(str((bal.get("used") or {}).get(self._quote) or 0))
        markets: dict[str, Any] = getattr(self._ex, "markets", None) or {}
        for ccy, amount in sorted(totals.items()):
            symbol = f"{ccy}/{self._quote}"
            if not amount or ccy == self._quote or symbol not in markets:
                continue
            t: dict[str, Any] = await self._call(lambda s=symbol: self._ex.fetch_ticker(s))  # type: ignore[misc]
            price = t.get("last") or t.get("close") or t.get("bid")
            if price:
                total += Decimal(str(amount)) * Decimal(str(price))
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

    # --- ордера (0.7.0) ----------------------------------------------------------------

    async def place_order(self, req: OrderRequest) -> OrderAck:
        """Одна попытка (ARCH §4.2). MARKET и LIMIT; STOP/STOP_LIMIT на spot не поддержаны."""
        if req.type not in ("MARKET", "LIMIT"):
            raise RejectedError(f"order type {req.type} not supported on spot")
        if req.type == "LIMIT" and req.limit_price is None:
            raise RejectedError("LIMIT order without limit_price")
        price = float(req.limit_price) if req.type == "LIMIT" and req.limit_price else None
        params: dict[str, Any] = {"clientOrderId": req.client_order_id}
        if req.type == "LIMIT":
            params["timeInForce"] = req.time_in_force

        async def create() -> dict[str, Any]:
            amount = float(req.qty)
            if hasattr(self._ex, "amount_to_precision"):  # ARCH §4.4: округление биржей
                amount = float(self._ex.amount_to_precision(req.symbol, amount))
            out: dict[str, Any] = await self._ex.create_order(
                req.symbol, req.type.lower(), req.side.lower(), amount, price, params
            )
            return out

        raw = await self._call(create, retry=False)
        return self._ack(raw, req.client_order_id, req.symbol)

    async def get_order(self, client_order_id: str, symbol: str) -> OrderAck | None:
        raw = await self._find(client_order_id, symbol)
        return None if raw is None else self._ack(raw, client_order_id, symbol)

    async def cancel_order(self, client_order_id: str, symbol: str) -> OrderAck:
        """Отмена по `clientOrderId`. Неизвестный ордер → `RejectedError`."""
        raw = await self._find(client_order_id, symbol)
        if raw is None:
            raise RejectedError(f"order not found: {client_order_id}")
        ack = self._ack(raw, client_order_id, symbol)
        if ack.state not in (OrderState.SUBMITTED, OrderState.PARTIAL):
            return ack  # уже терминальный
        venue_id = str(raw["id"])
        await self._call(lambda: self._ex.cancel_order(venue_id, symbol))
        final = await self._find(client_order_id, symbol)
        if final is None:
            return ack.model_copy(update={"state": OrderState.CANCELLED})
        return self._ack(final, client_order_id, symbol)

    async def _find(self, client_order_id: str, symbol: str) -> dict[str, Any] | None:
        has: dict[str, Any] = getattr(self._ex, "has", None) or {}
        if has.get("fetchOrders"):
            rows: list[dict[str, Any]] = await self._call(
                lambda: self._ex.fetch_orders(symbol, None, 100)
            )
        else:
            opened: list[dict[str, Any]] = await self._call(
                lambda: self._ex.fetch_open_orders(symbol)
            )
            closed: list[dict[str, Any]] = await self._call(
                lambda: self._ex.fetch_closed_orders(symbol, None, 100)
            )
            rows = [*opened, *closed]
        for row in rows:
            if row.get("clientOrderId") == client_order_id:
                return row
        return None

    def _ack(self, raw: dict[str, Any], client_order_id: str, symbol: str) -> OrderAck:
        def dec(x: Any) -> Decimal:
            return Decimal(str(x)) if x is not None else Decimal(0)

        filled = dec(raw.get("filled"))
        avg: Decimal | None = None
        if raw.get("average"):
            avg = dec(raw["average"])
        elif filled > 0 and raw.get("cost"):
            avg = dec(raw["cost"]) / filled
        status = raw.get("status")
        state = {
            "closed": OrderState.FILLED,
            "canceled": OrderState.CANCELLED,
            "cancelled": OrderState.CANCELLED,
            "expired": OrderState.CANCELLED,
            "rejected": OrderState.REJECTED,
        }.get(str(status), OrderState.PARTIAL if filled > 0 else OrderState.SUBMITTED)
        if state == OrderState.FILLED and filled <= 0:
            state = OrderState.SUBMITTED  # биржа ещё не отдала объём — ждём сверки
        base = symbol.split("/")[0]
        fees: list[dict[str, Any]] = list(raw.get("fees") or [])
        if not fees and raw.get("fee"):
            fees = [raw["fee"]]
        fee = Decimal(0)
        for f in fees:
            cost = dec(f.get("cost"))
            if f.get("currency") == base and avg is not None:
                cost *= avg  # комиссия в базовой валюте → в валюту котировки
            fee += cost
        return OrderAck(
            client_order_id=client_order_id,
            venue_order_id=None if raw.get("id") is None else str(raw["id"]),
            state=state,
            filled_qty=filled,
            avg_price=avg,
            fee=fee,
            fee_ccy=self._quote if fees else None,
            reject_reason=str(status) if state == OrderState.REJECTED else None,
            raw={k: raw.get(k) for k in ("id", "status", "filled", "average", "timestamp")},
        )
