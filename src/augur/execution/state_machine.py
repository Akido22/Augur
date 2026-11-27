"""Машина состояний ордера (ARCHITECTURE.md §7.3).

```
PENDING ─▶ SUBMITTED ─▶ PARTIAL ─▶ FILLED
   │           │            │
   ├─▶ REJECTED├─▶ CANCELLED ◀┘ (filled_qty сохраняется)
   └─▶ UNKNOWN ─▶ SUBMITTED/PARTIAL/FILLED/REJECTED/CANCELLED (reconcile)
```

Терминальные: FILLED, REJECTED, CANCELLED. Любой переход вне таблицы — `IllegalTransitionError`.
"""

from augur.adapters.base import OrderState

__all__ = [
    "ACTIVE_STATES",
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATES",
    "IllegalTransitionError",
    "check_transition",
    "path_to",
]

ALLOWED_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PENDING: frozenset({OrderState.SUBMITTED, OrderState.REJECTED, OrderState.UNKNOWN}),
    OrderState.SUBMITTED: frozenset(
        {
            OrderState.PARTIAL,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.PARTIAL: frozenset(
        {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELLED, OrderState.UNKNOWN}
    ),
    OrderState.UNKNOWN: frozenset(
        {
            OrderState.SUBMITTED,
            OrderState.PARTIAL,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
        }
    ),
    OrderState.FILLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.CANCELLED: frozenset(),
}

TERMINAL_STATES = frozenset(s for s, nxt in ALLOWED_TRANSITIONS.items() if not nxt)
ACTIVE_STATES = frozenset(
    {OrderState.PENDING, OrderState.SUBMITTED, OrderState.PARTIAL, OrderState.UNKNOWN}
)


class IllegalTransitionError(ValueError):
    """Переход состояния ордера не разрешён ARCH §7.3."""


def check_transition(current: OrderState, target: OrderState) -> None:
    """Raises: IllegalTransitionError — переход не из таблицы."""
    if target not in ALLOWED_TRANSITIONS[current]:
        raise IllegalTransitionError(f"{current.value} -> {target.value} is not allowed")


def path_to(current: OrderState, target: OrderState) -> list[OrderState]:
    """Цепочка разрешённых переходов от `current` к `target` (без `current`).

    Площадка отвечает итоговым статусом (`FILLED` сразу после `place_order`), а таблица
    требует `PENDING → SUBMITTED → FILLED`: промежуточные шаги проходятся явно, каждый пишется
    в аудит. Пустой список — `target == current` (кроме PARTIAL → PARTIAL, это новый fill).

    Raises:
        IllegalTransitionError: пути нет (например, из терминального состояния).
    """
    if target == current and target != OrderState.PARTIAL:
        return []
    if target in ALLOWED_TRANSITIONS[current]:
        return [target]
    for mid in (OrderState.SUBMITTED, OrderState.PARTIAL):
        if mid in ALLOWED_TRANSITIONS[current] and target in ALLOWED_TRANSITIONS[mid]:
            return [mid, target]
    raise IllegalTransitionError(f"{current.value} -> {target.value} is not reachable")
