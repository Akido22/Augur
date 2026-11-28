"""Сборка `RiskContext` из SQLite и адаптера (ARCHITECTURE.md §7.2, колонка «Источник данных»).

Новостной контур (0.8.0): активные строки `news_reactions` (не сняты, не истекли на `now`) →
`news_paused_symbols` / `news_paused_strategies` (R16) и `news_flags` символа (R15, soft).

Макро-календарь (0.12.0): события из `macro.calendar_path` с `impact ∈ blocking_impacts`,
затронувшие символ, в окне [время − before, время + after] → `macro_flags`
(`high_impact_event_imminent`/high, R15 блокирует вход). Календарь не читается →
`macro_calendar_invalid`/high (fail-closed). CLOSE/REDUCE R15 не блокирует.

Gate сам не ходит в БД/сеть; этот модуль — единственное место, где контекст собирается.
Только чтение. Рыночная часть (`MarketSnapshot`) приходит от адаптера через
`market_snapshot()`; z-оценка волатильности — `return_zscore()` по закрытым барам.
"""

import logging
import math
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from augur.adapters.base import InstrumentSpec, MarketAdapter, MarketStatus
from augur.config.settings import MacroSettings, TradingMode
from augur.data.calendar import CalendarError, load_calendar
from augur.data.models import Bar
from augur.fusion.contract import RiskFlag
from augur.risk.gate import intent_key
from augur.risk.models import CBState, RiskContext
from augur.signals.ready import SignalReady

__all__ = [
    "MODE_ORDER",
    "MarketSnapshot",
    "RiskContextBuilder",
    "market_snapshot",
    "return_zscore",
]

MODE_ORDER: tuple[TradingMode, ...] = (
    TradingMode.ANALYSIS_ONLY,
    TradingMode.SIGNAL_ONLY,
    TradingMode.PAPER,
    TradingMode.MICRO_LIVE,
    TradingMode.FULL_AUTO,
)
_ACTIVE_ORDER_STATES = ("PENDING", "SUBMITTED", "PARTIAL", "UNKNOWN")
MACRO_FLAG = "high_impact_event_imminent"
MACRO_INVALID_FLAG = "macro_calendar_invalid"
log = logging.getLogger(__name__)


class MarketSnapshot(BaseModel):
    """Рыночная часть контекста от адаптера. None — данных нет (чек не пройдёт)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    market_status: MarketStatus | None = None
    price: Decimal | None = None
    spread_bps: Decimal | None = None
    volatility_zscore: Decimal | None = None
    liquidity_quote: Decimal | None = None
    instrument: InstrumentSpec | None = None
    equity: Decimal | None = None
    cash: Decimal | None = None
    margin_used: Decimal = Decimal(0)


def return_zscore(bars: Sequence[Bar]) -> Decimal | None:
    """z-оценка последней лог-доходности относительно предыдущих (R7). < 3 доходностей → None."""
    closes = [b.close for b in bars if b.close > 0]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:], strict=False)]
    if len(rets) < 3:
        return None
    hist, last = rets[:-1], rets[-1]
    mean = sum(hist) / len(hist)
    var = sum((r - mean) ** 2 for r in hist) / (len(hist) - 1)
    if var <= 0:
        return Decimal(0) if last == mean else None
    return Decimal(str(round((last - mean) / math.sqrt(var), 6)))


async def market_snapshot(
    adapter: MarketAdapter,
    symbol: str,
    *,
    bars: Sequence[Bar] = (),
    liquidity_quote: Decimal | None = None,
) -> MarketSnapshot:
    """Снимок рынка из адаптера: статус, mid-цена, спред (bps), инструмент, счёт.

    `liquidity_quote` — если не передан, `tick.volume × mid` (24h объём, если площадка его даёт).
    """
    status = await adapter.market_status(symbol)
    tick = await adapter.get_latest(symbol)
    instrument = await adapter.get_instrument(symbol)
    account = await adapter.get_account_state()
    mid = (tick.bid + tick.ask) / 2 if tick.bid > 0 and tick.ask > 0 else None
    spread = (tick.ask - tick.bid) / mid * Decimal(10_000) if mid else None
    if liquidity_quote is None and tick.volume is not None and mid is not None:
        liquidity_quote = Decimal(str(tick.volume)) * mid
    return MarketSnapshot(
        market_status=status,
        price=mid,
        spread_bps=spread,
        volatility_zscore=return_zscore(bars),
        liquidity_quote=liquidity_quote,
        instrument=instrument,
        equity=account.equity,
        cash=account.cash,
        margin_used=account.margin_used,
    )


class RiskContextBuilder:
    """Читает `mode_state`, `cb_state`, `positions`, `portfolio_snapshots`, `orders`, `signals`,
    `news_reactions`.

    Args:
        conn: соединение с применёнными миграциями (0004+).
        configured_mode: `settings.trading_mode` — потолок режима (ARCH §7.1: при старте
            режим из `mode_state`, но не выше конфига).
        macro: настройки макро-календаря; None — календарь не учитывается.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        configured_mode: TradingMode,
        macro: MacroSettings | None = None,
    ) -> None:
        self._conn = conn
        self._configured = configured_mode
        self._macro = macro

    def current_mode(self) -> TradingMode:
        row = self._conn.execute("SELECT mode FROM mode_state WHERE id = 1").fetchone()
        if row is None:  # режим не выставлялся: не выше PAPER (повышение — только mode set)
            return min(self._configured, TradingMode.PAPER, key=MODE_ORDER.index)
        try:
            stored = TradingMode(row[0])
        except ValueError:
            return TradingMode.ANALYSIS_ONLY  # мусор в mode_state → самый безопасный режим
        return min(stored, self._configured, key=MODE_ORDER.index)

    def cb_state(self) -> CBState:
        """Нет строки — CB не срабатывал (ARMED)."""
        row = self._conn.execute("SELECT state FROM cb_state WHERE id = 1").fetchone()
        if row is None:
            return "ARMED"
        state = str(row[0])
        if state == "ARMED":
            return "ARMED"
        return "KILLED" if state == "KILLED" else "TRIPPED"

    def positions(self, mode: TradingMode) -> dict[str, Decimal]:
        rows = self._conn.execute(
            "SELECT symbol, qty FROM positions WHERE mode = ?", (mode.value,)
        ).fetchall()
        out: dict[str, Decimal] = {}
        for symbol, qty in rows:
            out[str(symbol)] = out.get(str(symbol), Decimal(0)) + Decimal(str(qty))
        return out

    def day_pnl(self, mode: TradingMode) -> tuple[Decimal | None, Decimal | None]:
        """(day_pnl, equity_start_of_day) из последнего снимка; equity_sod = equity − day_pnl."""
        row = self._conn.execute(
            "SELECT equity, day_pnl FROM portfolio_snapshots WHERE mode = ? "
            "ORDER BY ts DESC LIMIT 1",
            (mode.value,),
        ).fetchone()
        if row is None:
            return None, None
        equity, pnl = Decimal(str(row[0])), Decimal(str(row[1]))
        return pnl, equity - pnl

    def unknown_order_symbols(self) -> frozenset[str]:
        rows = self._conn.execute("SELECT DISTINCT symbol FROM orders WHERE state = 'UNKNOWN'")
        return frozenset(str(r[0]) for r in rows)

    def active_intents(self) -> frozenset[str]:
        marks = ",".join("?" * len(_ACTIVE_ORDER_STATES))
        rows = self._conn.execute(
            f"SELECT signal_id, side FROM orders WHERE state IN ({marks})",  # noqa: S608 — плейсхолдеры
            _ACTIVE_ORDER_STATES,
        )
        return frozenset(intent_key(str(s), str(side)) for s, side in rows)

    def signal_strategies(self, sig: SignalReady) -> frozenset[str]:
        if not sig.strategy_signal_ids:
            return frozenset()
        marks = ",".join("?" * len(sig.strategy_signal_ids))
        rows = self._conn.execute(
            f"SELECT strategy FROM signals WHERE kind='strategy' AND id IN ({marks})",  # noqa: S608
            sig.strategy_signal_ids,
        )
        return frozenset(str(r[0]) for r in rows if r[0])

    def news_effects(
        self, sig: SignalReady, now: datetime
    ) -> tuple[frozenset[str], frozenset[str], tuple[RiskFlag, ...]]:
        """(paused_symbols, paused_strategies ∩ стратегии сигнала, news-флаги символа).

        Пауза стратегии задаётся именем (`sma_cross`) или версией (`sma_cross@1.0.0`); имя
        покрывает все версии.
        """
        now_ms = int(now.timestamp() * 1000)
        rows = self._conn.execute(
            "SELECT action, symbol, strategy, flag_code, flag_severity FROM news_reactions "
            "WHERE lifted_ts IS NULL AND expires_ts > ? ORDER BY created_ts, id",
            (now_ms,),
        ).fetchall()
        symbols: set[str] = set()
        paused: set[str] = set()
        flags: list[RiskFlag] = []
        strategies = self.signal_strategies(sig)
        for action, symbol, strategy, code, severity in rows:
            if action == "pause_symbol" and symbol is not None:
                symbols.add(str(symbol))
            elif action == "pause_strategy" and strategy is not None:
                name = str(strategy)
                paused |= {s for s in strategies if s == name or s.split("@")[0] == name}
            elif action == "add_risk_flag" and symbol == sig.symbol and code is not None:
                flag = RiskFlag(code=str(code), severity=str(severity))  # type: ignore[arg-type]
                if flag not in flags:
                    flags.append(flag)
        return frozenset(symbols), frozenset(paused), tuple(flags)

    def macro_effects(
        self, symbol: str, now: datetime
    ) -> tuple[tuple[RiskFlag, ...], tuple[str, ...]]:
        """(macro-флаги, id событий) для символа на `now` (FUSION SPEC §3.4, ARCH §8.4)."""
        m = self._macro
        if m is None:
            return (), ()
        try:
            cal = load_calendar(m.calendar_path)
        except CalendarError as e:
            log.error("macro calendar unavailable, entries blocked: %s", e)
            return (RiskFlag(code=MACRO_INVALID_FLAG, severity="high"),), ()
        now_ms = int(now.timestamp() * 1000)
        events = cal.select(
            start_ms=now_ms - m.blackout_after_min * 60_000,
            end_ms=now_ms + m.blackout_before_min * 60_000,
            symbol=symbol,
            aliases=m.currency_aliases,
            impacts=m.blocking_impacts,
        )
        if not events:
            return (), ()
        return (RiskFlag(code=MACRO_FLAG, severity="high"),), tuple(e.id for e in events)

    def build(
        self,
        sig: SignalReady,
        market: MarketSnapshot,
        *,
        now: datetime,
        news_paused_symbols: frozenset[str] = frozenset(),
        news_paused_strategies: frozenset[str] = frozenset(),
    ) -> RiskContext:
        mode = self.current_mode()
        pnl, equity_sod = self.day_pnl(mode)
        db_symbols, db_strategies, news_flags = self.news_effects(sig, now)
        macro_flags, macro_ids = self.macro_effects(sig.symbol, now)
        return RiskContext(
            now=now,
            mode=mode,
            cb_state=self.cb_state(),
            **market.model_dump(),
            equity_start_of_day=equity_sod,
            day_pnl=pnl,
            positions=self.positions(mode),
            unknown_order_symbols=self.unknown_order_symbols(),
            active_intents=self.active_intents(),
            news_paused_symbols=news_paused_symbols | db_symbols,
            news_paused_strategies=news_paused_strategies | db_strategies,
            news_flags=news_flags,
            macro_flags=macro_flags,
            macro_event_ids=macro_ids,
            signal_strategies=self.signal_strategies(sig),
        )
