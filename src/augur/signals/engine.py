"""Signal Engine: стратегии → `StrategySignal` → Fusion → валидные `FusedSignal`.

ARCHITECTURE.md §2.5, §2.7, §5.5–5.6; ML_LLM_FUSION_SPEC.md §4.6, §6.3.

- Модуль MUST NOT импортировать ML-библиотеки и LLM SDK: вся ML/LLM-логика — за `FusionClient`.
- Fusion не обходит Risk Gate: `process()` только отбирает валидные сигналы, ничего не исполняет.
- Невалидный выход Fusion не «чинится»: он отбрасывается и пишется в `audit_log`
  (`FUSION_INVALID`); недоступный Fusion — `FUSION_FALLBACK` (режим ML-disabled).
"""

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from augur.data.models import Bar, timeframe_ms
from augur.db.audit import append_audit
from augur.fusion.client import FusionClient, FusionRequest
from augur.fusion.contract import FusedSignal
from augur.ids import ULID_RE, new_ulid
from augur.strategies.base import Signal, SignalAction

if TYPE_CHECKING:
    from augur.strategies.base import Strategy

__all__ = ["AUDIT_ACTOR", "SignalEngine", "StrategyContext", "StrategySignal"]

AUDIT_ACTOR = "signal_engine"
log = logging.getLogger(__name__)


def _sha256_json(obj: object) -> str:
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def params_hash(params: dict[str, Any]) -> str:
    """sha256 канонического JSON параметров (ключи отсортированы)."""
    return _sha256_json(params)


def data_hash(bars: Sequence[Bar]) -> str:
    """sha256 окна баров: (symbol, venue, timeframe, ts_open, OHLCV) по порядку."""
    return _sha256_json(
        [
            [b.symbol, b.venue, b.timeframe, b.ts_open, b.open, b.high, b.low, b.close, b.volume]
            for b in bars
        ]
    )


class StrategyContext(BaseModel):
    """Вход стратегии на одном закрытом баре. `timestamp` = `bar.ts_open`, Unix ms UTC.

    `history` — бары строго до `bar` (по возрастанию `ts_open`), без будущего.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bar: Bar
    history: list[Bar]
    symbol: str
    timeframe: str
    timestamp: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.bar.symbol != self.symbol or self.bar.timeframe != self.timeframe:
            raise ValueError("bar symbol/timeframe do not match context")
        if self.timestamp != self.bar.ts_open:
            raise ValueError("timestamp must equal bar.ts_open")
        if not self.bar.is_closed:
            raise ValueError("bar must be closed")
        if self.history and self.history[-1].ts_open >= self.bar.ts_open:
            raise ValueError("history must end strictly before bar (look-ahead)")
        return self


class StrategySignal(BaseModel):
    """Сигнал стратегии с идентификатором и хешами входа (ARCHITECTURE.md §2.5).

    `signal_id` — ULID, выдаётся Signal Engine. `timestamp` — `ts_open` бара решения.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_id: str = Field(pattern=ULID_RE.pattern)
    strategy_name: str
    strategy_version: str = Field(pattern=r"^[a-z0-9_]+@\d+\.\d+\.\d+$")  # name@SemVer
    params_hash: str
    data_hash: str
    symbol: str
    timeframe: str
    action: SignalAction
    strength: float = Field(ge=0.0, le=1.0)
    reason: str
    timestamp: int = Field(ge=0)

    @classmethod
    def from_signal(cls, signal: Signal, *, strategy: "Strategy", ctx: StrategyContext) -> Self:
        """Оборачивает `Signal` стратегии: ULID, `params_hash`, `data_hash`.

        `data_hash` считается по последним `strategy.warmup_bars` барам (включая текущий) —
        ровно по окну, от которого зависит решение стратегии.
        """
        window = max(1, strategy.warmup_bars)
        bars = [*ctx.history[-(window - 1) :], ctx.bar] if window > 1 else [ctx.bar]
        return cls(
            signal_id=new_ulid(),
            strategy_name=strategy.name,
            strategy_version=strategy.strategy_version,
            params_hash=params_hash(strategy.params),
            data_hash=data_hash(bars),
            symbol=ctx.symbol,
            timeframe=ctx.timeframe,
            action=signal.action,
            strength=signal.strength,
            reason=signal.reason,
            timestamp=signal.timestamp,
        )


class SignalEngine:
    """Прогон стратегий и приём сигналов Fusion.

    Args:
        fusion_client: граница с гибридным контуром (`StubFusionClient` в 0.4.0).
        audit_conn: соединение SQLite с таблицей `audit_log`; без него отбраковка
            пишется только в лог процесса.
        clock: текущее время, Unix ms UTC (инъекция для тестов и бэктеста).
    """

    def __init__(
        self,
        fusion_client: FusionClient,
        *,
        audit_conn: sqlite3.Connection | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._fusion = fusion_client
        self._audit_conn = audit_conn
        self._clock = clock or (lambda: int(time.time() * 1000))

    # --- стратегии --------------------------------------------------------------

    def generate_signals(
        self, strategy: "Strategy", bars: Sequence[Bar], symbol: str, timeframe: str
    ) -> list[StrategySignal]:
        """Прогоняет стратегию bar-by-bar от первого бара с полным warmup до конца.

        На баре i стратегия видит `bars[i]` и `bars[:i]` — без look-ahead.

        Raises:
            ValueError: бары другого инструмента/таймфрейма или не по возрастанию времени.
        """
        prev_ts = -1
        for b in bars:
            if b.symbol != symbol or b.timeframe != timeframe:
                raise ValueError("bars must match symbol and timeframe")
            if b.ts_open <= prev_ts:
                raise ValueError("bars must be strictly increasing by ts_open")
            prev_ts = b.ts_open
        out: list[StrategySignal] = []
        for i in range(max(0, strategy.warmup_bars - 1), len(bars)):
            ctx = StrategyContext(
                bar=bars[i],
                history=list(bars[:i]),
                symbol=symbol,
                timeframe=timeframe,
                timestamp=bars[i].ts_open,
            )
            out.extend(strategy.generate_signals(ctx))
        return out

    # --- Fusion -----------------------------------------------------------------

    @staticmethod
    def build_request(signal: StrategySignal) -> FusionRequest:
        """Запрос к Fusion по сигналу стратегии.

        `timestamp` — close_time бара; `idempotency_key` =
        sha256(asset | bar_close_ts | strategy_version | params_hash) — аналог
        ML_LLM_FUSION_SPEC.md §6.3 до появления ML-модели (0.10.0).
        """
        close_ts = signal.timestamp + timeframe_ms(signal.timeframe)
        key_src = f"{signal.symbol}|{close_ts}|{signal.strategy_version}|{signal.params_hash}"
        return FusionRequest(
            asset=signal.symbol,
            timeframe=signal.timeframe,
            timestamp=close_ts,
            idempotency_key=hashlib.sha256(key_src.encode("utf-8")).hexdigest(),
            context={
                "strategy_signal_id": signal.signal_id,
                "strategy_version": signal.strategy_version,
                "action": signal.action.value,
                "strength": signal.strength,
                "params_hash": signal.params_hash,
                "data_hash": signal.data_hash,
            },
        )

    def validate_fused(
        self, fused: FusedSignal, request: FusionRequest, now_ms: int
    ) -> tuple[FusedSignal | None, list[str]]:
        """Структурная валидация выхода Fusion (ARCHITECTURE.md §5.5). Ничего не исправляет.

        Повторно прогоняет Pydantic-валидацию (клиент мог собрать объект в обход неё),
        затем проверяет TTL и соответствие запросу.

        Returns:
            (валидный сигнал, []) или (None, список ошибок).
        """
        try:
            checked = FusedSignal.model_validate(fused.model_dump(mode="python"))
        except ValidationError as e:
            return None, [
                f"{'.'.join(map(str, err['loc'])) or 'model'}: {err['msg']}" for err in e.errors()
            ]
        errors: list[str] = []
        if checked.expires_at <= now_ms:
            errors.append(f"expired: expires_at {checked.expires_at} <= now {now_ms}")
        if checked.idempotency_key != request.idempotency_key:
            errors.append("idempotency_key does not match request")
        if checked.asset != request.asset:
            errors.append(f"asset mismatch: got {checked.asset}, expected {request.asset}")
        return (None, errors) if errors else (checked, [])

    async def process(self, signals: Sequence[StrategySignal]) -> list[FusedSignal]:
        """Для каждого сигнала стратегии запрашивает Fusion и возвращает только валидные.

        Невалидный выход (api_version, TTL, strength, пустой explanation_ref и т.п.) —
        отбрасывается, `FUSION_INVALID` в `audit_log`. Исключение клиента или None —
        `FUSION_FALLBACK` (ML-disabled), без повторов в цикле.
        """
        out: list[FusedSignal] = []
        for signal in signals:
            request = self.build_request(signal)
            try:
                fused = await self._fusion.get_fused_signal(request)
            except ValidationError as e:
                self._audit(
                    "FUSION_INVALID", signal, request, [f"client validation: {e.error_count()}"]
                )
                continue
            except Exception as e:  # noqa: BLE001 — сбой Fusion не роняет Signal Engine
                self._audit("FUSION_FALLBACK", signal, request, [f"{type(e).__name__}: {e}"])
                continue
            if fused is None:
                self._audit("FUSION_FALLBACK", signal, request, ["fusion returned no signal"])
                continue
            valid, errors = self.validate_fused(fused, request, self._clock())
            if valid is None:
                self._audit("FUSION_INVALID", signal, request, errors)
                continue
            out.append(valid)
        return out

    def _audit(
        self, event: str, signal: StrategySignal, request: FusionRequest, errors: list[str]
    ) -> None:
        payload = {
            "errors": errors,
            "idempotency_key": request.idempotency_key,
            "strategy_version": signal.strategy_version,
            "fallback": "ml_disabled",
        }
        log.warning("%s signal_id=%s errors=%s", event, signal.signal_id, errors)
        if self._audit_conn is None:
            return
        append_audit(
            self._audit_conn,
            actor=AUDIT_ACTOR,
            event=event,
            severity="WARN",
            ref_type="signal",
            ref_id=signal.signal_id,
            payload=payload,
            ts=self._clock(),
        )
        self._audit_conn.commit()
