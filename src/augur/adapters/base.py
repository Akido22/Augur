"""Унифицированный протокол рыночного адаптера (ARCHITECTURE.md §4.1).

Стратегии и Execution не знают о конкретном брокере: всё общение идёт через
`MarketAdapter`. Время — int, Unix ms UTC.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from augur.data.models import Bar, Tick


class OrderState(StrEnum):
    """Состояние ордера (совпадает с CHECK в `orders.state`)."""

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class MarketStatus(StrEnum):
    """Статус торгов по инструменту."""

    OPEN = "open"
    CLOSED = "closed"
    HALTED = "halted"


class OrderRequest(BaseModel):
    """Запрос на ордер. Объём и цены — `Decimal`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_order_id: str
    symbol: str  # каноническое имя
    side: Literal["BUY", "SELL"]
    type: Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT"]
    qty: Decimal = Field(gt=0)
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: Literal["GTC", "IOC", "FOK", "DAY"] = "GTC"
    reduce_only: bool = False
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None


class VenueFill(BaseModel):
    """Одно исполнение на площадке. `venue_fill_id` уникален в пределах ордера (дедуп fills)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    venue_fill_id: str
    qty: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    fee: Decimal = Decimal(0)
    ts: int  # Unix ms UTC


class OrderAck(BaseModel):
    """Ответ площадки на операцию с ордером.

    `fills` — все известные исполнения ордера (кумулятивно, не дельта). Если площадка их
    не отдаёт, Execution восстанавливает дельту по `filled_qty` / `avg_price`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_order_id: str
    venue_order_id: str | None
    state: OrderState
    filled_qty: Decimal = Decimal(0)
    avg_price: Decimal | None = None
    fee: Decimal = Decimal(0)  # суммарная комиссия по ордеру
    fee_ccy: str | None = None
    fills: list[VenueFill] = Field(default_factory=list)
    reject_reason: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)  # сырой ответ (без секретов) для аудита


class AccountState(BaseModel):
    """Состояние счёта. `ts` — Unix ms UTC."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    equity: Decimal
    cash: Decimal
    margin_used: Decimal
    currency: str
    ts: int


class InstrumentSpec(BaseModel):
    """Торговые параметры инструмента."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    qty_step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    price_tick: Decimal
    contract_size: Decimal = Decimal(1)
    trading_hours: str | None = None  # для акций/форекс


class VenuePosition(BaseModel):
    """Позиция на площадке. Знак `qty` — направление (+ long, − short)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    qty: Decimal
    avg_price: Decimal | None = None


# --- Ошибки адаптеров (ARCHITECTURE.md §4.2) ---------------------------------


class AdapterError(Exception):
    """Базовая ошибка адаптера."""


class RetryableError(AdapterError):
    """Сеть, 5xx, rate limit — можно повторить."""


class RejectedError(AdapterError):
    """Площадка отклонила запрос (средства, объём, символ) — без retry."""


class FatalAdapterError(AdapterError):
    """Авторизация/конфигурация — повтор бессмыслен, триггер CB."""


class AdapterNotConnectedError(AdapterError):
    """Метод вызван до `connect()`."""


ORDERS_NOT_SUPPORTED = "отправка ордеров через этот адаптер появится в 0.7.0 (MICRO_LIVE)"


@runtime_checkable
class MarketAdapter(Protocol):
    """Протокол адаптера площадки. Все методы асинхронные."""

    venue: str

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def get_historical(self, symbol: str, timeframe: str, start: int, end: int) -> list[Bar]:
        """Бары с `start <= ts_open < end` (Unix ms UTC), отсортированные по `ts_open`."""
        ...

    async def get_latest(self, symbol: str) -> Tick: ...

    async def place_order(self, req: OrderRequest) -> OrderAck: ...

    async def cancel_order(self, client_order_id: str, symbol: str) -> OrderAck: ...

    async def get_order(self, client_order_id: str, symbol: str) -> OrderAck | None: ...

    async def get_positions(self) -> list[VenuePosition]: ...

    async def get_account_state(self) -> AccountState: ...

    async def get_instrument(self, symbol: str) -> InstrumentSpec: ...

    async def market_status(self, symbol: str) -> MarketStatus: ...
