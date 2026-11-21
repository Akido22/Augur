"""Адаптеры площадок (ARCHITECTURE.md §4).

Публичный API — протокол и DTO из `base`, политика вызовов и `PaperAdapter`.
Реальные адаптеры (`mt5`, `ccxt_adapter`, `alpaca`) импортируются из своих модулей;
их SDK — опциональные extras и подгружаются лениво при `connect()`.
"""

from augur.adapters.base import (
    AccountState,
    AdapterError,
    AdapterNotConnectedError,
    FatalAdapterError,
    InstrumentSpec,
    MarketAdapter,
    MarketStatus,
    OrderAck,
    OrderRequest,
    OrderState,
    RejectedError,
    RetryableError,
    VenuePosition,
)
from augur.adapters.paper import PaperAdapter
from augur.adapters.resilient import CallPolicy, call_with_retry

__all__ = [
    "AccountState",
    "AdapterError",
    "AdapterNotConnectedError",
    "CallPolicy",
    "FatalAdapterError",
    "InstrumentSpec",
    "MarketAdapter",
    "MarketStatus",
    "OrderAck",
    "OrderRequest",
    "OrderState",
    "PaperAdapter",
    "RejectedError",
    "RetryableError",
    "VenuePosition",
    "call_with_retry",
]
