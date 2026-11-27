"""PaperAdapter — симулятор площадки без сети (ARCHITECTURE.md §4.6).

Котировки — детерминированное синтетическое случайное блуждание (seed + символ +
таймфрейм), либо, если передан `price_source`, бары и котировки берутся из
реального адаптера только на чтение. Ордера исполняются сразу (`FILLED`) по
текущей цене; повтор с тем же `client_order_id` возвращает исходный ответ.

Издержки (0.6.0) — общая с бэктестом `CostModel` (ARCHITECTURE.md §4.6, §6.3): цена
исполнения = bid/ask с проскальзыванием, комиссия = notional × c списывается из cash.
По умолчанию издержки нулевые; Execution передаёт `CostModel` из `settings.execution`.

Состояние (кэш, позиции) живёт в памяти; после рестарта процесса Execution восстанавливает
его из SQLite через `restore()` — иначе Reconciler увидит расхождение позиций.
"""

import hashlib
import random
import time
from decimal import Decimal

from augur.adapters.base import (
    AccountState,
    InstrumentSpec,
    MarketAdapter,
    MarketStatus,
    OrderAck,
    OrderRequest,
    OrderState,
    RejectedError,
    VenueFill,
    VenuePosition,
)
from augur.backtest.costs import CostModel
from augur.data.models import Bar, Tick, timeframe_ms

_PRICE_Q = Decimal("0.00000001")


def _now_ms() -> int:
    return int(time.time() * 1000)


class PaperAdapter:
    """Внутренний paper-брокер, реализует `MarketAdapter`."""

    def __init__(
        self,
        *,
        seed: int = 42,
        start_cash: Decimal = Decimal("10000"),
        currency: str = "USDT",
        base_price: float = 100.0,
        price_source: MarketAdapter | None = None,
        clock_ms: int | None = None,
        costs: CostModel | None = None,
    ) -> None:
        """
        Args:
            seed: seed синтетического генератора (одинаковый seed → одинаковые бары).
            start_cash: стартовый кэш счёта.
            currency: валюта счёта.
            base_price: опорная цена синтетики.
            price_source: реальный адаптер для котировок (только чтение) или None.
            clock_ms: фиксированное «сейчас» (Unix ms) для детерминированных тестов.
            costs: комиссия и проскальзывание; None — без издержек.
        """
        self.venue = "paper"
        self._seed = seed
        self._cash = start_cash
        self._currency = currency
        self._base_price = base_price
        self._source = price_source
        self._clock_ms = clock_ms
        self._positions: dict[str, tuple[Decimal, Decimal]] = {}  # symbol -> (qty, avg_price)
        self._orders: dict[str, OrderAck] = {}
        self._last_price: dict[str, Decimal] = {}
        self._costs = costs or CostModel()

    def restore(self, *, cash: Decimal, positions: list[VenuePosition]) -> None:
        """Восстановить кэш и позиции (после рестарта процесса; ордера не восстанавливаются)."""
        self._cash = cash
        self._positions = {
            p.symbol: (p.qty, p.avg_price or Decimal(0)) for p in positions if p.qty != 0
        }

    def set_clock(self, clock_ms: int | None) -> None:
        """Переставить фиксированное «сейчас» (тесты, replay)."""
        self._clock_ms = clock_ms

    # --- жизненный цикл -------------------------------------------------------

    async def connect(self) -> None:
        if self._source is not None:
            await self._source.connect()

    async def close(self) -> None:
        if self._source is not None:
            await self._source.close()

    def _now(self) -> int:
        return self._clock_ms if self._clock_ms is not None else _now_ms()

    # --- рыночные данные ------------------------------------------------------

    def _synthetic_bar(self, symbol: str, timeframe: str, ts_open: int) -> Bar:
        """Детерминированный бар: зависит только от seed, символа, таймфрейма и времени."""
        key = f"{self._seed}|{symbol}|{timeframe}|{ts_open}".encode()
        rng = random.Random(int.from_bytes(hashlib.sha256(key).digest()[:8], "big"))  # noqa: S311
        step = ts_open // timeframe_ms(timeframe)
        trend = 1.0 + 0.1 * ((step % 200) / 200.0)  # медленная «волна», чтобы цена не была плоской
        open_ = self._base_price * trend * (1 + rng.uniform(-0.01, 0.01))
        close = open_ * (1 + rng.uniform(-0.01, 0.01))
        high = max(open_, close) * (1 + rng.uniform(0, 0.005))
        low = min(open_, close) * (1 - rng.uniform(0, 0.005))
        return Bar(
            symbol=symbol,
            venue=self.venue,
            timeframe=timeframe,
            ts_open=ts_open,
            open=round(open_, 8),
            high=round(high, 8),
            low=round(low, 8),
            close=round(close, 8),
            volume=round(rng.uniform(10, 1000), 4),
            is_closed=ts_open + timeframe_ms(timeframe) <= self._now(),
        )

    async def get_historical(self, symbol: str, timeframe: str, start: int, end: int) -> list[Bar]:
        if self._source is not None:
            return await self._source.get_historical(symbol, timeframe, start, end)
        tf = timeframe_ms(timeframe)
        first = -(-start // tf) * tf  # выравнивание вверх на границу бара
        return [self._synthetic_bar(symbol, timeframe, ts) for ts in range(first, end, tf)]

    async def get_latest(self, symbol: str) -> Tick:
        if self._source is not None:
            tick = await self._source.get_latest(symbol)
        else:
            now = self._now()
            tf = timeframe_ms("1m")
            bar = self._synthetic_bar(symbol, "1m", now // tf * tf)
            mid = Decimal(str(bar.close))
            spread = (mid * Decimal("0.0001")).quantize(_PRICE_Q)
            tick = Tick(
                symbol=symbol,
                venue=self.venue,
                ts=now,
                bid=mid - spread,
                ask=mid + spread,
                last=mid,
                volume=round(bar.volume * 1440, 4),  # «24h-объём» синтетики (R9 Risk Gate)
            )
        self._last_price[symbol] = tick.last if tick.last is not None else (tick.bid + tick.ask) / 2
        return tick

    # --- ордера ---------------------------------------------------------------

    async def place_order(self, req: OrderRequest) -> OrderAck:
        """Мгновенное исполнение по текущей цене. Идемпотентно по `client_order_id`."""
        existing = self._orders.get(req.client_order_id)
        if existing is not None:
            return existing
        tick = await self.get_latest(req.symbol)
        ref = tick.ask if req.side == "BUY" else tick.bid
        price = self._costs.fill_price(req.side, ref).quantize(_PRICE_Q)
        signed = req.qty if req.side == "BUY" else -req.qty
        cost = (price * signed).quantize(_PRICE_Q)
        fee = self._costs.fee(cost).quantize(_PRICE_Q)
        qty, avg = self._positions.get(req.symbol, (Decimal(0), Decimal(0)))
        if req.reduce_only and (qty == 0 or (qty > 0) == (signed > 0) or abs(signed) > abs(qty)):
            raise RejectedError(f"reduce_only would increase position: {qty} {req.side} {req.qty}")
        if req.side == "BUY" and cost + fee > self._cash:
            raise RejectedError(f"insufficient funds: need {cost + fee}, have {self._cash}")
        new_qty = qty + signed
        if new_qty == 0:
            self._positions.pop(req.symbol, None)
        else:
            if qty == 0 or (qty > 0) == (signed > 0):
                new_avg = (qty * avg + signed * price) / new_qty  # открытие / добор
            elif (qty > 0) == (new_qty > 0):
                new_avg = avg  # частичное закрытие: средняя не меняется
            else:
                new_avg = price  # переворот: остаток открыт по текущей цене
            self._positions[req.symbol] = (new_qty, new_avg.quantize(_PRICE_Q))
        self._cash -= cost + fee
        venue_order_id = f"paper-{len(self._orders) + 1}"
        ack = OrderAck(
            client_order_id=req.client_order_id,
            venue_order_id=venue_order_id,
            state=OrderState.FILLED,
            filled_qty=req.qty,
            avg_price=price,
            fee=fee,
            fee_ccy=self._currency,
            fills=[
                VenueFill(
                    venue_fill_id=f"{venue_order_id}-1",
                    qty=req.qty,
                    price=price,
                    fee=fee,
                    ts=tick.ts,
                )
            ],
            raw={"venue": self.venue, "ts": tick.ts},
        )
        self._orders[req.client_order_id] = ack
        return ack

    async def cancel_order(self, client_order_id: str, symbol: str) -> OrderAck:
        """Paper-ордера исполняются мгновенно — отменять нечего; возвращает текущий статус."""
        ack = self._orders.get(client_order_id)
        if ack is None:
            raise RejectedError(f"unknown client_order_id: {client_order_id}")
        return ack

    async def get_order(self, client_order_id: str, symbol: str) -> OrderAck | None:
        return self._orders.get(client_order_id)

    # --- счёт -----------------------------------------------------------------

    async def get_positions(self) -> list[VenuePosition]:
        return [
            VenuePosition(symbol=s, qty=q, avg_price=a)
            for s, (q, a) in sorted(self._positions.items())
        ]

    async def get_account_state(self) -> AccountState:
        market_value = Decimal(0)
        for symbol, (qty, avg) in self._positions.items():
            market_value += qty * self._last_price.get(symbol, avg)
        return AccountState(
            equity=(self._cash + market_value).quantize(_PRICE_Q),
            cash=self._cash,
            margin_used=Decimal(0),
            currency=self._currency,
            ts=self._now(),
        )

    async def get_instrument(self, symbol: str) -> InstrumentSpec:
        return InstrumentSpec(
            symbol=symbol,
            qty_step=Decimal("0.00000001"),
            min_qty=Decimal("0.00000001"),
            min_notional=Decimal(0),
            price_tick=Decimal("0.00000001"),
        )

    async def market_status(self, symbol: str) -> MarketStatus:
        return MarketStatus.OPEN
