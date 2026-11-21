"""Политика вызовов адаптера: таймаут, retry с экспоненциальной паузой и jitter.

ARCHITECTURE.md §4.2. `place_order` вслепую не ретраится никогда: при таймауте
Execution сначала спрашивает `get_order(client_order_id)` (этап 0.6.0).
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field

from augur.adapters.base import RetryableError

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
) -> T:
    """Вызывает `fn` с таймаутом; повторяет только `RetryableError` и таймаут.

    Args:
        fn: фабрика корутины (вызывается заново на каждую попытку).
        policy: таймаут и параметры повторов.
        sleep: функция паузы (подменяется в тестах).

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
            if attempt < policy.max_retries:
                await sleep(backoff_delay(attempt, policy))
    raise RetryableError(f"retries exhausted after {policy.max_retries + 1} attempts") from last
