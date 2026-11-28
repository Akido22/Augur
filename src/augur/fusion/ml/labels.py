"""Triple-barrier разметка (ML_LLM_FUSION_SPEC.md §2.1; skill ml-training-inference, п. 4).

Вход — закрытие бара `t`. Барьеры: `close_t ± k·ATR_t`, вертикальный — через `h` баров.
Класс задаёт первое касание на барах `t+1 … t+h`; оба барьера в одном баре или вертикальный
барьер → `flat`. Последние `h` баров без полной разметки получают `None`.
"""

import numpy as np
import numpy.typing as npt

__all__ = ["FLAT", "LONG", "SHORT", "triple_barrier"]

SHORT, FLAT, LONG = 0, 1, 2  # индексы классов LightGBM; порядок = CLASS_NAMES


def triple_barrier(
    high: npt.NDArray[np.float64],
    low: npt.NDArray[np.float64],
    close: npt.NDArray[np.float64],
    atr: npt.NDArray[np.float64],
    *,
    k: float,
    h: int,
) -> list[int | None]:
    """Метка для каждого бара; `atr[t]` NaN/≤0 или нет `h` будущих баров → None."""
    n = close.shape[0]
    out: list[int | None] = [None] * n
    for t in range(n - h):
        a = float(atr[t])
        if not np.isfinite(a) or a <= 0:
            continue
        up = float(close[t]) + k * a
        dn = float(close[t]) - k * a
        label = FLAT
        for j in range(t + 1, t + h + 1):
            hit_up = float(high[j]) >= up
            hit_dn = float(low[j]) <= dn
            if hit_up and hit_dn:
                label = FLAT
                break
            if hit_up:
                label = LONG
                break
            if hit_dn:
                label = SHORT
                break
        out[t] = label
    return out
