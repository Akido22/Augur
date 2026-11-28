"""Risk Gate: pre-trade checks R1–R18 и sizing (ARCHITECTURE.md §2.8, §7.2).

Нормативно:
- Порядок чеков фиксирован, выполняются ВСЕ (без short-circuit) — в аудите полный список причин.
- Gate может только уменьшить объём или отклонить сигнал; лимиты — из `RiskLimits` (конфиг).
- Вердикт — чистая функция `(SignalReady, RiskContext, RiskLimits)`: без I/O и системных часов
  (время — `ctx.now`). Нет данных → чек не проходит (fail-closed).
- `risk_flags` (`{code, severity}`) только ужесточают: R15 блокирует вход, soft-флаги режут объём.
  Флаги — `sig.risk_flags` ∪ `ctx.news_flags` (реакции новостного контура, ARCH §8.4)
  ∪ `ctx.macro_flags` (макро-календарь, 0.12.0: вход в окне high-impact события блокируется
  детерминированно, без LLM — FUSION SPEC §3.4).

Действия, снижающие риск (`CLOSE`, `REDUCE`), не блокируются чеками, защищающими от НОВОГО
риска [ДОПУЩЕНИЕ, skill auto-trade-risk-gate: «CB останавливает только новые входы»]:

- R1, R3, R4, R5, R6, R8, R9, R13, R17 — для любых действий;
- R2 — вход только при `ARMED`; CLOSE/REDUCE также при `TRIPPED` (не при `KILLED`);
- R7, R10, R11, R12, R14, R15, R16, R18 — только для входа; для CLOSE/REDUCE `passed` с
  причиной `not applicable`.

`HOLD` / `NO_SIGNAL` проверяются как вход и всегда отклоняются R4.
"""

from collections.abc import Callable
from decimal import Decimal

from augur.adapters.base import MarketStatus
from augur.config.settings import RiskLimits, TradingMode
from augur.fusion.contract import RiskFlag
from augur.ids import new_ulid
from augur.risk.models import CheckResult, RiskContext, RiskVerdict, VerdictKind
from augur.risk.sizing import FixedFractionalSizer, PositionSizer, notional, round_down_to_step
from augur.signals.ready import SignalReady
from augur.strategies.base import SignalAction

__all__ = ["CHECK_NAMES", "RiskGate", "SizingResult", "intent_key", "order_side"]

CHECK_NAMES: tuple[str, ...] = (
    "mode_allows_trading",  # R1
    "circuit_breaker_armed",  # R2
    "signal_ttl",  # R3
    "action_actionable",  # R4
    "no_unknown_orders",  # R5
    "market_open",  # R6
    "abnormal_volatility",  # R7
    "spread",  # R8
    "liquidity",  # R9
    "daily_loss",  # R10
    "max_open_positions",  # R11
    "concentration",  # R12
    "position_size",  # R13
    "margin_available",  # R14
    "risk_flags",  # R15
    "news_pause",  # R16
    "duplicate_intent",  # R17
    "micro_live_cap",  # R18
)

_TRADING_MODES = frozenset({TradingMode.PAPER, TradingMode.MICRO_LIVE, TradingMode.FULL_AUTO})
_OPENING = frozenset({SignalAction.BUY, SignalAction.SELL})
_REDUCING = frozenset({SignalAction.CLOSE, SignalAction.REDUCE})
_NA = "not applicable: risk-reducing action"
_NO_DATA = "no data"


def _flags(sig: SignalReady, ctx: RiskContext) -> list[RiskFlag]:
    """Флаги сигнала, news- и macro-флаги символа (дубликаты не мешают: работаем по множествам)."""
    return [*sig.risk_flags, *ctx.news_flags, *ctx.macro_flags]


def order_side(sig: SignalReady, position_qty: Decimal) -> str | None:
    """Сторона ордера: BUY/SELL как есть; CLOSE/REDUCE — против позиции; иначе None."""
    if sig.action in _OPENING:
        return sig.action.value
    if sig.action in _REDUCING and position_qty != 0:
        return "SELL" if position_qty > 0 else "BUY"
    return None


def intent_key(signal_id: str, side: str) -> str:
    """Ключ активного намерения для R17: `"<signal_id>|<side>"`."""
    return f"{signal_id}|{side}"


def _s(v: object) -> str | None:
    return None if v is None else str(v)


class SizingResult:
    """Итог sizing: объём после всех ограничений и признак уменьшения soft-флагом."""

    __slots__ = ("notional", "qty", "reason", "soft_codes")

    def __init__(
        self,
        qty: Decimal | None,
        notional_value: Decimal | None,
        soft_codes: list[str],
        reason: str | None,
    ) -> None:
        self.qty = qty
        self.notional = notional_value
        self.soft_codes = soft_codes
        self.reason = reason


class RiskGate:
    """Последний детерминированный барьер перед Execution.

    Args:
        limits: `settings.risk`.
        sizer: по умолчанию `FixedFractionalSizer(limits)`.
        id_factory: генератор `verdict_id` (ULID); инъекция для golden-тестов.
    """

    def __init__(
        self,
        limits: RiskLimits,
        *,
        sizer: PositionSizer | None = None,
        id_factory: Callable[[], str] = new_ulid,
    ) -> None:
        self._limits = limits
        self._sizer = sizer or FixedFractionalSizer(limits)
        self._new_id = id_factory

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    async def evaluate(self, sig: SignalReady, ctx: RiskContext) -> RiskVerdict:
        """Вердикт по сигналу (ARCHITECTURE.md §2.8). Не исполняет и не пишет в БД."""
        return self.evaluate_sync(sig, ctx)

    def evaluate_sync(self, sig: SignalReady, ctx: RiskContext) -> RiskVerdict:
        """Синхронная версия `evaluate` (бэктест, replay)."""
        reducing = sig.action in _REDUCING
        sizing = self.size(sig, ctx)
        checks = [
            self._r1_mode(ctx),
            self._r2_cb(ctx, reducing),
            self._r3_ttl(sig, ctx),
            self._r4_actionable(sig),
            self._r5_unknown(sig, ctx),
            self._r6_market(ctx),
            self._r7_volatility(ctx, reducing),
            self._r8_spread(ctx),
            self._r9_liquidity(ctx),
            self._r10_daily_loss(ctx, reducing),
            self._r11_open_positions(sig, ctx, reducing),
            self._r12_concentration(sig, ctx, reducing, sizing),
            self._r13_position_size(sig, ctx, reducing, sizing),
            self._r14_margin(ctx, reducing, sizing),
            self._r15_flags(sig, ctx, reducing),
            self._r16_news(sig, ctx, reducing),
            self._r17_duplicate(sig, ctx),
            self._r18_micro(ctx, reducing, sizing),
        ]
        assert tuple(c.name for c in checks) == CHECK_NAMES  # noqa: S101 — порядок фиксирован
        failed = [c for c in checks if not c.passed]
        approved = not failed
        kind: VerdictKind
        if not approved:
            kind = "REJECTED"
            reason: str | None = "; ".join(f"{c.name}: {c.reason or 'failed'}" for c in failed)
        elif sizing.soft_codes:
            kind = "REDUCED"
            reason = (
                f"soft_flags {','.join(sizing.soft_codes)}: "
                f"qty x {self._limits.risk_flag_multiplier}"
            )
        else:
            kind, reason = "APPROVED", None
        return RiskVerdict(
            verdict_id=self._new_id(),
            signal_id=sig.signal_id,
            approved=approved,
            risk_verdict=kind,
            risk_reason=reason,
            checks=checks,
            sized_qty=sizing.qty if approved else None,
            sized_notional=sizing.notional if approved else None,
            mode=ctx.mode,
            evaluated_at=ctx.now,
        )

    # --- sizing ---------------------------------------------------------------------

    def size(self, sig: SignalReady, ctx: RiskContext) -> SizingResult:
        """Объём после всех ограничений (до проверки R13).

        Вход: sizer → cap `max_position_pct_equity` → MICRO_LIVE cap → soft-флаги → `qty_step`.
        CLOSE: |позиция|; REDUCE: |позиция| × `reduce_fraction`. Округление — только вниз.
        """
        inst, price = ctx.instrument, ctx.price
        if inst is None or price is None:
            return SizingResult(None, None, [], "no instrument/price")
        if sig.action in _REDUCING:
            pos = abs(ctx.positions.get(sig.symbol, Decimal(0)))
            if pos == 0:
                return SizingResult(None, None, [], "no open position to reduce")
            raw = pos if sig.action == SignalAction.CLOSE else pos * self._limits.reduce_fraction
            qty = round_down_to_step(min(raw, pos), inst.qty_step)
            return SizingResult(qty, notional(qty, price, inst), [], None)
        if sig.action not in _OPENING:
            return SizingResult(None, None, [], "action not actionable")
        if ctx.equity is None or ctx.equity <= 0:
            return SizingResult(None, None, [], "no equity")
        unit = price * inst.contract_size
        qty = self._sizer.size(sig, ctx.equity, price, sig.stop_loss, inst)
        qty = min(qty, ctx.equity * self._limits.max_position_pct_equity / unit)
        if ctx.mode == TradingMode.MICRO_LIVE:
            qty = min(qty, self._limits.micro_live_max_notional / unit)
        soft = sorted({f.code for f in _flags(sig, ctx)} & set(self._limits.soft_flags))
        if soft:
            qty = qty * self._limits.risk_flag_multiplier
        qty = round_down_to_step(qty, inst.qty_step)
        return SizingResult(qty, notional(qty, price, inst), soft, None)

    # --- чеки R1–R18 ------------------------------------------------------------------

    @staticmethod
    def _r1_mode(ctx: RiskContext) -> CheckResult:
        ok = ctx.mode in _TRADING_MODES
        return CheckResult(
            name="mode_allows_trading",
            passed=ok,
            value=ctx.mode.value,
            limit="PAPER|MICRO_LIVE|FULL_AUTO",
            reason=None if ok else f"mode {ctx.mode.value} does not trade",
        )

    @staticmethod
    def _r2_cb(ctx: RiskContext, reducing: bool) -> CheckResult:
        ok = ctx.cb_state == "ARMED" or (reducing and ctx.cb_state == "TRIPPED")
        return CheckResult(
            name="circuit_breaker_armed",
            passed=ok,
            value=ctx.cb_state,
            limit="ARMED",
            reason=None if ok else f"circuit breaker {ctx.cb_state}",
        )

    @staticmethod
    def _r3_ttl(sig: SignalReady, ctx: RiskContext) -> CheckResult:
        ok = ctx.now < sig.expires_at
        return CheckResult(
            name="signal_ttl",
            passed=ok,
            value=ctx.now.isoformat(),
            limit=sig.expires_at.isoformat(),
            reason=None if ok else "signal expired",
        )

    @staticmethod
    def _r4_actionable(sig: SignalReady) -> CheckResult:
        ok = sig.action not in (SignalAction.HOLD, SignalAction.NO_SIGNAL)
        return CheckResult(
            name="action_actionable",
            passed=ok,
            value=sig.action.value,
            reason=None if ok else f"{sig.action.value} is not actionable",
        )

    @staticmethod
    def _r5_unknown(sig: SignalReady, ctx: RiskContext) -> CheckResult:
        ok = sig.symbol not in ctx.unknown_order_symbols
        return CheckResult(
            name="no_unknown_orders",
            passed=ok,
            value=sig.symbol,
            reason=None if ok else "orders in UNKNOWN state for symbol",
        )

    @staticmethod
    def _r6_market(ctx: RiskContext) -> CheckResult:
        st = ctx.market_status
        ok = st == MarketStatus.OPEN
        return CheckResult(
            name="market_open",
            passed=ok,
            value=None if st is None else st.value,
            limit=MarketStatus.OPEN.value,
            reason=None if ok else (_NO_DATA if st is None else f"market {st.value}"),
        )

    def _r7_volatility(self, ctx: RiskContext, reducing: bool) -> CheckResult:
        limit = self._limits.volatility_halt_zscore
        z = ctx.volatility_zscore
        if reducing:
            return CheckResult(name="abnormal_volatility", passed=True, value=_s(z), reason=_NA)
        ok = z is not None and abs(z) < limit
        return CheckResult(
            name="abnormal_volatility",
            passed=ok,
            value=_s(z),
            limit=str(limit),
            reason=None if ok else (_NO_DATA if z is None else "|z| at or above halt threshold"),
        )

    def _r8_spread(self, ctx: RiskContext) -> CheckResult:
        limit = self._limits.max_spread_bps
        v = ctx.spread_bps
        ok = v is not None and v <= limit
        return CheckResult(
            name="spread",
            passed=ok,
            value=_s(v),
            limit=str(limit),
            reason=None if ok else (_NO_DATA if v is None else "spread_bps above limit"),
        )

    def _r9_liquidity(self, ctx: RiskContext) -> CheckResult:
        limit = self._limits.min_liquidity_quote
        v = ctx.liquidity_quote
        ok = v is not None and v >= limit
        return CheckResult(
            name="liquidity",
            passed=ok,
            value=_s(v),
            limit=str(limit),
            reason=None if ok else (_NO_DATA if v is None else "liquidity below minimum"),
        )

    def _r10_daily_loss(self, ctx: RiskContext, reducing: bool) -> CheckResult:
        if reducing:
            return CheckResult(name="daily_loss", passed=True, value=_s(ctx.day_pnl), reason=_NA)
        if ctx.day_pnl is None or ctx.equity_start_of_day is None:
            return CheckResult(name="daily_loss", passed=False, reason=_NO_DATA)
        floor = -self._limits.max_daily_loss_pct * ctx.equity_start_of_day
        ok = ctx.day_pnl > floor
        return CheckResult(
            name="daily_loss",
            passed=ok,
            value=str(ctx.day_pnl),
            limit=str(floor),
            reason=None if ok else "daily loss limit reached",
        )

    def _r11_open_positions(
        self, sig: SignalReady, ctx: RiskContext, reducing: bool
    ) -> CheckResult:
        open_pos = ctx.open_positions
        count = len(open_pos)
        limit = self._limits.max_open_positions
        if reducing:
            return CheckResult(name="max_open_positions", passed=True, value=str(count), reason=_NA)
        ok = sig.symbol in open_pos or count < limit
        return CheckResult(
            name="max_open_positions",
            passed=ok,
            value=str(count),
            limit=str(limit),
            reason=None if ok else "open positions limit reached",
        )

    def _r12_concentration(
        self, sig: SignalReady, ctx: RiskContext, reducing: bool, sizing: SizingResult
    ) -> CheckResult:
        limit = self._limits.max_symbol_concentration_pct
        if reducing:
            return CheckResult(name="concentration", passed=True, reason=_NA)
        if (
            sizing.notional is None
            or ctx.equity is None
            or ctx.equity <= 0
            or ctx.price is None
            or ctx.instrument is None
        ):
            return CheckResult(
                name="concentration", passed=False, limit=str(limit), reason=_NO_DATA
            )
        current = notional(
            abs(ctx.positions.get(sig.symbol, Decimal(0))), ctx.price, ctx.instrument
        )
        ratio = (current + sizing.notional) / ctx.equity
        ok = ratio <= limit
        return CheckResult(
            name="concentration",
            passed=ok,
            value=str(ratio),
            limit=str(limit),
            reason=None if ok else "symbol concentration above limit",
        )

    def _r13_position_size(
        self, sig: SignalReady, ctx: RiskContext, reducing: bool, sizing: SizingResult
    ) -> CheckResult:
        name = "position_size"
        inst = ctx.instrument
        if sizing.qty is None or sizing.notional is None or inst is None:
            return CheckResult(name=name, passed=False, reason=sizing.reason or _NO_DATA)
        qty, value = sizing.qty, sizing.notional
        problems: list[str] = []
        limit: str | None = None
        if qty <= 0:
            problems.append("sized_qty is zero")
        if qty < inst.min_qty:
            problems.append(f"qty below min_qty {inst.min_qty}")
        if value < inst.min_notional:
            problems.append(f"notional below min_notional {inst.min_notional}")
        if reducing:
            pos = abs(ctx.positions.get(sig.symbol, Decimal(0)))
            limit = str(pos)
            if qty > pos:
                problems.append("qty exceeds open position")
        elif ctx.equity is None:
            problems.append(_NO_DATA)
        else:
            cap = self._limits.max_position_pct_equity * ctx.equity
            limit = str(cap)
            if value > cap:
                problems.append("notional above max_position_pct_equity")
        return CheckResult(
            name=name,
            passed=not problems,
            value=str(qty),
            limit=limit,
            reason="; ".join(problems) if problems else None,
        )

    @staticmethod
    def _r14_margin(ctx: RiskContext, reducing: bool, sizing: SizingResult) -> CheckResult:
        if reducing:
            return CheckResult(name="margin_available", passed=True, reason=_NA)
        if ctx.cash is None or sizing.notional is None:
            return CheckResult(name="margin_available", passed=False, reason=_NO_DATA)
        available = ctx.cash - ctx.margin_used
        ok = sizing.notional <= available
        return CheckResult(
            name="margin_available",
            passed=ok,
            value=str(sizing.notional),
            limit=str(available),
            reason=None if ok else "insufficient cash/margin",
        )

    def _r15_flags(self, sig: SignalReady, ctx: RiskContext, reducing: bool) -> CheckResult:
        flags = _flags(sig, ctx)
        codes = sorted({f.code for f in flags})
        if reducing:
            return CheckResult(name="risk_flags", passed=True, value=",".join(codes), reason=_NA)
        blocking = set(self._limits.blocking_flags) | set(
            self._limits.blocking_flags_by_mode.get(ctx.mode, [])
        )
        hits = sorted({f.code for f in flags if f.code in blocking})
        high = sorted({f.code for f in flags if f.severity == "high"})
        problems = []
        if hits:
            problems.append(f"blocking flags: {','.join(hits)}")
        if high:
            problems.append(f"high severity: {','.join(high)}")
        if ctx.macro_event_ids:
            problems.append(f"macro events: {','.join(ctx.macro_event_ids)}")
        return CheckResult(
            name="risk_flags",
            passed=not problems,
            value=",".join(codes),
            limit=",".join(sorted(blocking)),
            reason="; ".join(problems) if problems else None,
        )

    @staticmethod
    def _r16_news(sig: SignalReady, ctx: RiskContext, reducing: bool) -> CheckResult:
        if reducing:
            return CheckResult(name="news_pause", passed=True, reason=_NA)
        paused_strats = sorted(ctx.signal_strategies & ctx.news_paused_strategies)
        problems = []
        if sig.symbol in ctx.news_paused_symbols:
            problems.append(f"symbol {sig.symbol} paused")
        if paused_strats:
            problems.append(f"strategies paused: {','.join(paused_strats)}")
        return CheckResult(
            name="news_pause",
            passed=not problems,
            reason="; ".join(problems) if problems else None,
        )

    @staticmethod
    def _r17_duplicate(sig: SignalReady, ctx: RiskContext) -> CheckResult:
        side = order_side(sig, ctx.positions.get(sig.symbol, Decimal(0)))
        if side is None:
            return CheckResult(name="duplicate_intent", passed=True, reason="no order side")
        key = intent_key(sig.signal_id, side)
        ok = key not in ctx.active_intents
        return CheckResult(
            name="duplicate_intent",
            passed=ok,
            value=key,
            reason=None if ok else "active order with same side for this signal",
        )

    def _r18_micro(self, ctx: RiskContext, reducing: bool, sizing: SizingResult) -> CheckResult:
        limit = self._limits.micro_live_max_notional
        if ctx.mode != TradingMode.MICRO_LIVE:
            return CheckResult(name="micro_live_cap", passed=True, reason="not MICRO_LIVE")
        if reducing:
            return CheckResult(name="micro_live_cap", passed=True, reason=_NA)
        if sizing.notional is None:
            return CheckResult(
                name="micro_live_cap", passed=False, limit=str(limit), reason=_NO_DATA
            )
        ok = sizing.notional <= limit
        return CheckResult(
            name="micro_live_cap",
            passed=ok,
            value=str(sizing.notional),
            limit=str(limit),
            reason=None if ok else "notional above micro_live_max_notional",
        )
