"""Калибровка вероятностей one-vs-rest (ML_LLM_FUSION_SPEC.md §2.3, §2.6; skill п. 7).

Isotonic (PAV) при ≥ `isotonic_min` наблюдений, иначе Platt. Параметры хранятся в JSON
(`calibrator.json`) и применяются этим кодом — без pickle sklearn-объектов. После калибровки
вероятности нормируются к сумме 1.
"""

import math
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Calibrator", "ClassCalibration", "fit_calibrator"]

F64 = npt.NDArray[np.float64]
_EPS = 1e-6


class ClassCalibration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["isotonic", "platt"]
    x: list[float] = Field(default_factory=list)  # isotonic: пороги (возрастают)
    y: list[float] = Field(default_factory=list)  # isotonic: значения
    a: float = 0.0  # platt: p = sigmoid(a·logit(raw) + b)
    b: float = 0.0

    def apply(self, raw: F64) -> F64:
        if self.method == "isotonic":
            out: F64 = np.interp(raw, np.array(self.x), np.array(self.y))
            return out
        z = np.clip(raw, _EPS, 1 - _EPS)
        logit = np.log(z / (1 - z))
        return 1.0 / (1.0 + np.exp(-(self.a * logit + self.b)))


class Calibrator(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    classes: list[ClassCalibration] = Field(min_length=2)

    def apply(self, raw: F64) -> F64:
        """raw: (n, C) сырые вероятности → (n, C) калиброванные, строки суммируются в 1."""
        if raw.ndim != 2 or raw.shape[1] != len(self.classes):
            raise ValueError("raw probabilities shape does not match calibrator")
        cal = np.column_stack([c.apply(raw[:, j]) for j, c in enumerate(self.classes)])
        cal = np.clip(cal, _EPS, None)
        out: F64 = cal / cal.sum(axis=1, keepdims=True)
        return out

    def to_json_obj(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _pav(x: F64, y: F64) -> tuple[list[float], list[float]]:
    """Изотоническая регрессия (pool adjacent violators), неубывающая по x."""
    order = np.argsort(x, kind="mergesort")
    xs, ys = x[order], y[order]
    blocks: list[list[float]] = []  # [sum_y, count, x_lo, x_hi]
    for xi, yi in zip(xs.tolist(), ys.tolist(), strict=True):
        blocks.append([yi, 1.0, xi, xi])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, c, lo, hi = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
            blocks[-1][3] = hi
            del lo
    px: list[float] = []
    py: list[float] = []
    for s, c, lo, hi in blocks:
        val = s / c
        px.extend([lo, hi])
        py.extend([val, val])
    return px, py


def _platt(raw: F64, y: F64) -> tuple[float, float]:
    """Логистическая регрессия y ~ sigmoid(a·logit(raw) + b), Ньютон с малой L2."""
    z = np.clip(raw, _EPS, 1 - _EPS)
    f = np.log(z / (1 - z))
    a, b = 1.0, 0.0
    for _ in range(50):
        p = 1.0 / (1.0 + np.exp(-(a * f + b)))
        w = p * (1 - p) + 1e-9
        ga = float(np.sum((p - y) * f)) + 1e-3 * a
        gb = float(np.sum(p - y))
        haa = float(np.sum(w * f * f)) + 1e-3
        hab = float(np.sum(w * f))
        hbb = float(np.sum(w)) + 1e-9
        det = haa * hbb - hab * hab
        if det <= 0 or not math.isfinite(det):
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        a, b = a - da, b - db
        if abs(da) + abs(db) < 1e-10:
            break
    return float(a), float(b)


def fit_calibrator(raw: F64, labels: npt.NDArray[np.int64], *, isotonic_min: int) -> Calibrator:
    """Обучает калибратор на калибровочном фолде (тест не трогается)."""
    n, c = raw.shape
    if n == 0:
        raise ValueError("empty calibration fold")
    method: Literal["isotonic", "platt"] = "isotonic" if n >= isotonic_min else "platt"
    classes: list[ClassCalibration] = []
    for j in range(c):
        y = (labels == j).astype(np.float64)
        if method == "isotonic":
            px, py = _pav(raw[:, j], y)
            classes.append(ClassCalibration(method="isotonic", x=px, y=py))
        else:
            a, b = _platt(raw[:, j], y)
            classes.append(ClassCalibration(method="platt", a=a, b=b))
    return Calibrator(classes=classes)
