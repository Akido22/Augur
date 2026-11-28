"""Purged walk-forward с embargo (ML_LLM_FUSION_SPEC.md §2.3, López de Prado).

Индексы — позиции в выборке по времени. Purge `horizon` между train → calib и calib → test
(метки triple-barrier смотрят на `horizon` баров вперёд), embargo после теста —
`max(horizon, 1 % n, embargo)`. Случайный K-fold не поддерживается намеренно.
"""

from collections.abc import Iterator
from dataclasses import dataclass

__all__ = ["Fold", "purged_walk_forward"]


@dataclass(frozen=True)
class Fold:
    train: range
    calib: range
    test: range


def purged_walk_forward(
    n: int,
    *,
    train: int,
    test: int,
    horizon: int,
    calib_fraction: float,
    calib_min: int,
    embargo: int = 0,
    expanding: bool = False,
) -> Iterator[Fold]:
    """Фолды без утечек: train < calib < test, между ними ≥ `horizon` пропущенных индексов."""
    emb = max(horizon, n // 100, embargo)
    start = 0
    while start + train + test <= n:
        tr_lo = 0 if expanding else start
        tr_hi = start + train - horizon  # purge: последняя метка calib не заходит в test
        calib_len = max(int(calib_fraction * (tr_hi - tr_lo)), calib_min)
        calib_lo = tr_hi - calib_len
        train_hi = calib_lo - horizon  # purge между train и calib
        if train_hi - tr_lo <= 0 or calib_lo < tr_lo:
            raise ValueError("train window too small for calib_min/horizon")
        te_lo, te_hi = start + train, start + train + test
        yield Fold(range(tr_lo, train_hi), range(calib_lo, tr_hi), range(te_lo, te_hi))
        start += test + emb
