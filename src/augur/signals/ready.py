"""`SignalReady` — единственный вход Risk Gate (ARCHITECTURE.md §2.7, §5.5–5.6).

- `combine_classic()` — детерминированная агрегация классических сигналов без ML.
- `SignalReady.from_fused()` — итоговый сигнал из валидного `FusedSignal` (+ V7 флаг).
- Флаги — только `RiskFlag {code, severity}` (FUSION SPEC §4.6); флаги могут лишь ужесточать
  решение Risk Gate (чек R15 и soft-флаги, ARCHITECTURE.md §7.2).

Модуль MUST NOT содержать ML-кода и вызывать LLM.
"""

from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from augur.data.models import timeframe_ms
from augur.fusion.contract import FusedSignal, RiskFlag
from augur.ids import ULID_RE, new_ulid
from augur.strategies.base import SignalAction

if TYPE_CHECKING:
    from augur.signals.engine import StrategySignal

__all__ = [
    "DECISION_AUDIT_REF_PREFIX",
    "FLAG_FUSION_CONTRARIAN",
    "FLAG_ML_DISABLED",
    "SignalReady",
    "SignalSource",
    "combine_classic",
    "decision_id_from_ref",
    "ms_to_dt",
]

SignalSource = Literal["classic", "fusion", "classic_fallback"]

# Системные флаги Signal Engine (ARCHITECTURE.md §7.2).
FLAG_ML_DISABLED = RiskFlag(code="ml_disabled", severity="low")
FLAG_FUSION_CONTRARIAN = RiskFlag(code="fusion_contrarian", severity="medium")

# explanation_ref = "decision_audit:<ulid>" (FUSION SPEC §4.6, 0.5.0+): строка-ссылка, НЕ FK.
DECISION_AUDIT_REF_PREFIX = "decision_audit:"

# При равном числе голосов выигрывает действие с меньшим риском (раньше в кортеже).
_TIE_PRIORITY: tuple[SignalAction, ...] = (
    SignalAction.CLOSE,
    SignalAction.REDUCE,
    SignalAction.HOLD,
    SignalAction.NO_SIGNAL,
    SignalAction.BUY,
    SignalAction.SELL,
)


def ms_to_dt(ms: int) -> datetime:
    """Unix ms UTC → aware `datetime` (UTC)."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def decision_id_from_ref(explanation_ref: str | None) -> str | None:
    """ULID записи `decision_audit` из `explanation_ref="decision_audit:<ulid>"`, иначе None."""
    if explanation_ref is None or not explanation_ref.startswith(DECISION_AUDIT_REF_PREFIX):
        return None
    candidate = explanation_ref[len(DECISION_AUDIT_REF_PREFIX) :]
    return candidate if ULID_RE.fullmatch(candidate) else None


class SignalReady(BaseModel):
    """Событие `signal_ready` (ARCHITECTURE.md §2.7). Время — aware `datetime` UTC.

    Инварианты:
    - `source="fusion"` ⇔ задан `fused_signal_id` и непустой `explanation_ref`;
      `classic` / `classic_fallback` — оба None.
    - `expires_at > created_at`; `strength ∈ [0, 1]`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_id: str = Field(pattern=ULID_RE.pattern)
    symbol: str = Field(min_length=1)
    action: SignalAction
    strength: float = Field(ge=0.0, le=1.0)
    source: SignalSource
    explanation_ref: str | None
    risk_flags: list[RiskFlag]
    strategy_signal_ids: list[str]
    fused_signal_id: str | None
    created_at: datetime
    expires_at: datetime
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None

    @field_validator("created_at", "expires_at")
    @classmethod
    def _aware_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware (UTC)")
        return v.astimezone(UTC)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be > created_at")
        if self.source == "fusion":
            if self.fused_signal_id is None:
                raise ValueError("source=fusion requires fused_signal_id")
            if not (self.explanation_ref or "").strip():
                raise ValueError("source=fusion requires non-empty explanation_ref")
        elif self.fused_signal_id is not None or self.explanation_ref is not None:
            raise ValueError(f"source={self.source} must not carry fused_signal_id/explanation_ref")
        return self

    # --- преобразования (возвращают новый объект; модель frozen) ------------------

    def with_source(self, source: Literal["classic", "classic_fallback"]) -> "SignalReady":
        """Копия классического сигнала с другим `source` (fusion получают через `from_fused`)."""
        return self.model_validate({**self.model_dump(), "source": source})

    def add_flag(self, flag: RiskFlag) -> "SignalReady":
        """Копия с добавленным флагом (дубликат по `code` и `severity` не добавляется)."""
        if flag in self.risk_flags:
            return self
        return self.model_validate({**self.model_dump(), "risk_flags": [*self.risk_flags, flag]})

    @classmethod
    def from_fused(
        cls,
        fused: FusedSignal,
        *,
        fused_id: str,
        parents: Sequence["StrategySignal"],
        now_ms: int,
    ) -> "SignalReady":
        """Итоговый сигнал из валидного `FusedSignal` (ARCHITECTURE.md §5.6).

        V7 (§5.5): `BUY`/`SELL` без согласного классического сигнала → флаг
        `fusion_contrarian` (medium). Сигнал при этом не меняется и не считается невалидным.
        """
        flags = list(fused.risk_flags)
        if fused.action in (SignalAction.BUY, SignalAction.SELL) and not any(
            p.action == fused.action for p in parents
        ):
            if FLAG_FUSION_CONTRARIAN not in flags:
                flags.append(FLAG_FUSION_CONTRARIAN)
        return cls(
            signal_id=new_ulid(now_ms),
            symbol=fused.asset,
            action=fused.action,
            strength=fused.strength,
            source="fusion",
            explanation_ref=fused.explanation_ref,
            risk_flags=flags,
            strategy_signal_ids=[p.signal_id for p in parents],
            fused_signal_id=fused_id,
            created_at=ms_to_dt(now_ms),
            expires_at=ms_to_dt(fused.expires_at),
        )


def combine_classic(signals: Sequence["StrategySignal"], *, now_ms: int) -> SignalReady | None:
    """Детерминированная агрегация классических сигналов одного бара (ARCHITECTURE.md §2.7).

    - Нет сигналов → None.
    - Есть и `BUY`, и `SELL` → `HOLD`, `strength=0` (конфликт направлений).
    - Иначе побеждает действие большинства; при равенстве — менее рискованное
      (CLOSE > REDUCE > HOLD > NO_SIGNAL > BUY > SELL); `strength` = max по победившему действию.
    - TTL: `created_at + ½ бара` (FUSION SPEC §6.3).

    Raises:
        ValueError: сигналы разных инструментов, таймфреймов или баров.
    """
    if not signals:
        return None
    first = signals[0]
    for s in signals[1:]:
        if (s.symbol, s.timeframe, s.timestamp) != (first.symbol, first.timeframe, first.timestamp):
            raise ValueError("combine_classic: signals must share symbol, timeframe and bar")
    actions = {s.action for s in signals}
    if SignalAction.BUY in actions and SignalAction.SELL in actions:
        action, strength = SignalAction.HOLD, 0.0
    else:
        votes = Counter(s.action for s in signals)
        top = max(votes.values())
        action = next(a for a in _TIE_PRIORITY if votes.get(a, 0) == top)
        strength = max(s.strength for s in signals if s.action == action)
    ttl = max(1, timeframe_ms(first.timeframe) // 2)
    return SignalReady(
        signal_id=new_ulid(now_ms),
        symbol=first.symbol,
        action=action,
        strength=strength,
        source="classic",
        explanation_ref=None,
        risk_flags=[],
        strategy_signal_ids=[s.signal_id for s in signals],
        fused_signal_id=None,
        created_at=ms_to_dt(now_ms),
        expires_at=ms_to_dt(now_ms + ttl),
    )
