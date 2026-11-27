"""Политика вызовов адаптера: таймаут, retry с экспоненциальной паузой и jitter.

ARCHITECTURE.md §4.2. `place_order` вслепую не ретраится никогда: при таймауте
Execution сначала спрашивает `get_order(client_order_id)` (`OrderManager.submit`, 0.6.0).

`ResilientAdapter` — декоратор над `MarketAdapter`: чтение и `cancel_order`/`get_order`
(идемпотентны по `client_order_id`) повторяются по `CallPolicy`; `place_order` — одна попытка
с таймаутом. Каждая ошибка площадки передаётся в `on_error` (счётчик CB-триггера
`adapter_errors`, ARCH §7.4). Execution работает ТОЛЬКО с `ResilientAdapter`.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field

from augur.adapters.base import (
    AccountState,
    AdapterError,
    InstrumentSpec,
    MarketAdapter,
    MarketStatus,
    OrderAck,
    OrderRequest,
    RejectedError,
    RetryableError,
    VenuePosition,
)
from augur.data.models import Bar, Tick

ErrorHook = Callable[[BaseException], None]

T = TypeVar("T")


class CallPolicy(BaseModel):
    """Параметры повторов. `max_retries` — число повторов сверх первой попытки."""

    model_config = ConfigDict(frozen=True)

    timeout_s: float = Field(default=10.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    backoff_base_s: float = Field(default=0.5, ge=0)
    backoff_max_s: float = Field(default=10.0, ge=0)


def backoff_delay(attempt: int, policy: CallPolicy, rng: random.Random | None = None) -> float:
    """Пауза перед повтором `attempt` (0-based): base·2^attempt с полным jitter, не больше max."""
    cap = min(policy.backoff_max_s, policy.backoff_base_s * (2**attempt))
    return (rng or random).uniform(0, cap)  # noqa: S311 — jitter, не криптография


async def call_with_retry(
    fn: Callable[[], Awaitable[T]],
    policy: CallPolicy,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_error: ErrorHook | None = None,
) -> T:
    """Вызывает `fn` с таймаутом; повторяет только `RetryableError` и таймаут.

    Args:
        fn: фабрика корутины (вызывается заново на каждую попытку).
        policy: таймаут и параметры повторов.
        sleep: функция паузы (подменяется в тестах).
        on_error: вызывается на каждую ошибку площадки, кроме `RejectedError`.

    Returns:
        Результат `fn`.

    Raises:
        RetryableError: попытки исчерпаны (исходная ошибка — в `__cause__`).
        AdapterError: неретраимая ошибка пробрасывается сразу.
    """
    last: BaseException | None = None
    for attempt in range(policy.max_retries + 1):
        try:
            return await asyncio.wait_for(fn(), timeout=policy.timeout_s)
        except (RetryableError, TimeoutError) as e:
            last = e
            if on_error is not None:
                on_error(e)
            if attempt < policy.max_retries:
                await sleep(backoff_delay(attempt, policy))
        except RejectedError:
            raise  # отказ площадки по существу запроса — не сбой адаптера
        except AdapterError as e:
            if on_error is not None:
                on_error(e)
            raise
    raise RetryableError(f"retries exhausted after {policy.max_retries + 1} attempts") from last


class ResilientAdapter:
    """`MarketAdapter` с политикой вызовов ARCH §4.2.

    Args:
        inner: адаптер площадки.
        policy: таймаут и повторы.
        on_error: хук на каждую ошибку площадки (CB `adapter_errors`); `RejectedError` — нет.
        sleep: функция паузы (тесты).
    """

    def __init__(
        self,
        inner: MarketAdapter,
        policy: CallPolicy | None = None,
        *,
        on_error: ErrorHook | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._inner = inner
        self._policy = policy or CallPolicy()
        self._sleep = sleep
        self.on_error = on_error
        self.venue = inner.venue

    @property
    def inner(self) -> MarketAdapter:
        return self._inner

    def _report(self, e: BaseException) -> None:
        if self.on_error is not None:
            self.on_error(e)

    async def _call(self, fn: Callable[[], Awaitable[T]]) -> T:
        return await call_with_retry(fn, self._policy, sleep=self._sleep, on_error=self._report)

    async def connect(self) -> None:
        await self._call(self._inner.connect)

    async def close(self) -> None:
        await self._inner.close()

    async def get_historical(self, symbol: str, timeframe: str, start: int, end: int) -> list[Bar]:
        return await self._call(lambda: self._inner.get_historical(symbol, timeframe, start, end))

    async def get_latest(self, symbol: str) -> Tick:
        return await self._call(lambda: self._inner.get_latest(symbol))

    async def place_order(self, req: OrderRequest) -> OrderAck:
        """Одна попытка с таймаутом. Таймаут → `RetryableError` (статус ордера неизвестен)."""
        try:
            return await asyncio.wait_for(
                self._inner.place_order(req), timeout=self._policy.timeout_s
            )
        except TimeoutError as e:
            self._report(e)
            raise RetryableError(f"place_order timeout: {req.client_order_id}") from e
        except RejectedError:
            raise
        except AdapterError as e:
            self._report(e)
            raise

    async def cancel_order(self, client_order_id: str, symbol: str) -> OrderAck:
        return await self._call(lambda: self._inner.cancel_order(client_order_id, symbol))

    async def get_order(self, client_order_id: str, symbol: str) -> OrderAck | None:
        return await self._call(lambda: self._inner.get_order(client_order_id, symbol))

    async def get_positions(self) -> list[VenuePosition]:
        return await self._call(self._inner.get_positions)

    async def get_account_state(self) -> AccountState:
        return await self._call(self._inner.get_account_state)

    async def get_instrument(self, symbol: str) -> InstrumentSpec:
        return await self._call(lambda: self._inner.get_instrument(symbol))

    async def market_status(self, symbol: str) -> MarketStatus:
        return await self._call(lambda: self._inner.market_status(symbol))
