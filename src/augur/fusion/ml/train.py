"""Обучение классификатора long/flat/short (ML_LLM_FUSION_SPEC.md §2.1–2.4; skill п. 3–8).

1. Признаки — `compute_features` на каждом закрытом баре (та же функция, что в инференсе).
2. Метки — triple-barrier (`k·ATR`, `h` баров); последние `h` баров не размечаются.
3. Оценка — purged walk-forward с embargo: на каждом фолде LightGBM на train, калибратор на
   calib, метрики на test (log-loss, Brier, ECE, Sharpe и max DD с издержками `CostModel`).
   Гиперпараметры фиксированы конфигом → `trials = 1`.
4. Итоговая модель — на всей выборке с тем же разбиением train → purge → calib.
Детерминизм: `seed`, `deterministic=True`, `force_row_wise=True`, `num_threads=1`.
"""

import math
import platform
import subprocess
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from augur.backtest.costs import CostModel
from augur.config.settings import MLSettings
from augur.data.models import timeframe_ms
from augur.fusion.ml.artifact import write_artifact
from augur.fusion.ml.calibration import Calibrator, fit_calibrator
from augur.fusion.ml.features import (
    BarArrays,
    FeatureVector,
    compute_features,
    feature_names,
    feature_set_version,
)
from augur.fusion.ml.labels import FLAT, LONG, SHORT, triple_barrier
from augur.fusion.ml.walk_forward import purged_walk_forward

__all__ = ["Dataset", "TrainResult", "build_dataset", "horizon_label", "train_model"]

F64 = npt.NDArray[np.float64]
_YEAR_MS = 365 * 86_400_000
_ECE_BINS = 10


@dataclass(frozen=True)
class Dataset:
    x: F64  # (n, F)
    y: npt.NDArray[np.int64]  # (n,)
    bar_idx: npt.NDArray[np.int64]  # индекс бара строки в BarArrays
    as_of: npt.NDArray[np.int64]  # close_time бара строки


@dataclass(frozen=True)
class TrainResult:
    version: str
    path: Path
    sha256: str
    meta: dict[str, Any]
    metrics: dict[str, float]


def horizon_label(timeframe: str, h: int) -> str:
    """Горизонт в формате `^[0-9]+(m|h|d)$` (FUSION SPEC §2.5)."""
    total = timeframe_ms(timeframe) * h
    for unit, ms in (("d", 86_400_000), ("h", 3_600_000)):
        if total % ms == 0:
            return f"{total // ms}{unit}"
    return f"{total // 60_000}m"


def build_dataset(bars: BarArrays, cfg: MLSettings) -> Dataset:
    """Строки, у которых есть и признаки, и полная triple-barrier метка."""
    n = len(bars)
    fvs: list[FeatureVector | None] = [
        compute_features(bars, bars.close_time(i), cfg.features) for i in range(n)
    ]
    atr = np.array(
        [fv.values["atr_rel"] * float(bars.close[i]) if fv else np.nan for i, fv in enumerate(fvs)]
    )
    labels = triple_barrier(
        bars.high, bars.low, bars.close, atr, k=cfg.train.barrier_k, h=cfg.train.horizon_bars
    )
    rows = [i for i in range(n) if fvs[i] is not None and labels[i] is not None]
    if not rows:
        return Dataset(
            x=np.zeros((0, len(feature_names(cfg.features)))),
            y=np.zeros(0, dtype=np.int64),
            bar_idx=np.zeros(0, dtype=np.int64),
            as_of=np.zeros(0, dtype=np.int64),
        )
    x = np.array([fvs[i].as_list() for i in rows], dtype=np.float64)  # type: ignore[union-attr]
    y = np.array([labels[i] for i in rows], dtype=np.int64)
    return Dataset(
        x=x,
        y=y,
        bar_idx=np.array(rows, dtype=np.int64),
        as_of=np.array([bars.close_time(i) for i in rows], dtype=np.int64),
    )


def _lgb_params(cfg: MLSettings) -> dict[str, Any]:
    t = cfg.train
    return {
        "objective": "multiclass",
        "num_class": 3,
        "num_leaves": t.num_leaves,
        "learning_rate": t.learning_rate,
        "min_data_in_leaf": t.min_data_in_leaf,
        "seed": t.seed,
        "deterministic": True,
        "force_row_wise": True,
        "num_threads": 1,
        "verbosity": -1,
    }


def _fit(x: F64, y: npt.NDArray[np.int64], cfg: MLSettings) -> Any:
    import lightgbm

    ds = lightgbm.Dataset(x, label=y, feature_name=feature_names(cfg.features), free_raw_data=True)
    return lightgbm.train(_lgb_params(cfg), ds, num_boost_round=cfg.train.n_estimators)


def _predict_raw(booster: Any, x: F64) -> F64:
    return np.asarray(booster.predict(x, num_threads=1), dtype=np.float64)


def _calib_split(n: int, cfg: MLSettings) -> tuple[range, range]:
    h = cfg.train.horizon_bars
    calib_len = max(int(cfg.train.calib_fraction * n), cfg.train.calib_min)
    calib_lo = n - calib_len
    if calib_lo - h <= 0:
        raise ValueError("not enough rows for train + calibration")
    return range(0, calib_lo - h), range(calib_lo, n)


def _metrics(
    probs: F64,
    y: npt.NDArray[np.int64],
    rets: F64,
    *,
    min_conf: float,
    cost_rate: float,
    bars_per_year: float,
) -> dict[str, float]:
    n = y.shape[0]
    p_true = np.clip(probs[np.arange(n), y], 1e-12, 1.0)
    logloss = float(-np.mean(np.log(p_true)))
    onehot = np.zeros_like(probs)
    onehot[np.arange(n), y] = 1.0
    brier = float(np.mean(np.sum((probs - onehot) ** 2, axis=1)))
    conf = probs.max(axis=1)
    pred = probs.argmax(axis=1)
    ece = 0.0
    for b in range(_ECE_BINS):
        lo, hi = b / _ECE_BINS, (b + 1) / _ECE_BINS
        m = (conf > lo) & (conf <= hi) if b else (conf >= lo) & (conf <= hi)
        if m.any():
            ece += float(m.mean()) * abs(float((pred[m] == y[m]).mean()) - float(conf[m].mean()))
    pos = np.where((pred == LONG) & (conf >= min_conf), 1.0, 0.0) - np.where(
        (pred == SHORT) & (conf >= min_conf), 1.0, 0.0
    )
    turnover = np.abs(np.diff(np.concatenate(([0.0], pos))))
    pnl = pos * rets - turnover * cost_rate
    sd = float(np.std(pnl))
    sharpe = float(np.mean(pnl)) / sd * math.sqrt(bars_per_year) if sd > 0 else 0.0
    equity = np.cumprod(1.0 + pnl)
    peak = np.maximum.accumulate(np.concatenate(([1.0], equity)))[1:]
    max_dd = float(np.max(1.0 - equity / peak)) if equity.size else 0.0
    return {
        "logloss": round(logloss, 6),
        "brier": round(brier, 6),
        "ece": round(ece, 6),
        "sharpe": round(sharpe, 6),
        "max_dd": round(max_dd, 6),
        "accuracy": round(float((pred == y).mean()), 6),
        "trades_share": round(float(np.mean(pos != 0)), 6),
    }


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 — фиксированная команда
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def _lib_versions() -> dict[str, str]:
    out = {"python": platform.python_version()}
    for lib in ("lightgbm", "numpy"):
        try:
            out[lib] = version(lib)
        except PackageNotFoundError:
            out[lib] = "missing"
    return out


def train_model(
    bars: BarArrays,
    *,
    asset: str,
    name: str,
    version: str,
    cfg: MLSettings,
    min_confidence: float,
    costs: CostModel,
    artifacts_dir: Path,
) -> TrainResult:
    """Walk-forward оценка + итоговая модель → неизменяемый артефакт (без регистрации)."""
    ds = build_dataset(bars, cfg)
    n = ds.y.shape[0]
    t = cfg.train
    nxt = np.minimum(ds.bar_idx + 1, len(bars) - 1)
    rets = bars.close[nxt] / bars.close[ds.bar_idx] - 1.0
    bars_per_year = _YEAR_MS / timeframe_ms(bars.timeframe)
    cost_rate = costs.commission_rate + costs.slippage_rate

    probs_all: list[F64] = []
    idx_all: list[int] = []
    folds = 0
    for fold in purged_walk_forward(
        n,
        train=t.train_bars,
        test=t.test_bars,
        horizon=t.horizon_bars,
        calib_fraction=t.calib_fraction,
        calib_min=t.calib_min,
        expanding=t.expanding,
    ):
        tr, ca, te = list(fold.train), list(fold.calib), list(fold.test)
        booster = _fit(ds.x[tr], ds.y[tr], cfg)
        cal = fit_calibrator(_predict_raw(booster, ds.x[ca]), ds.y[ca], isotonic_min=t.isotonic_min)
        probs_all.append(cal.apply(_predict_raw(booster, ds.x[te])))
        idx_all.extend(te)
        folds += 1
    if folds == 0:
        raise ValueError(
            f"not enough labelled rows ({n}) for walk-forward "
            f"(train_bars={t.train_bars}, test_bars={t.test_bars})"
        )
    test_idx = np.array(idx_all, dtype=np.int64)
    metrics = _metrics(
        np.vstack(probs_all),
        ds.y[test_idx],
        rets[test_idx],
        min_conf=min_confidence,
        cost_rate=cost_rate,
        bars_per_year=bars_per_year,
    )
    metrics.update({"folds": folds, "trials": 1, "test_rows": int(test_idx.size)})

    tr_range, ca_range = _calib_split(n, cfg)
    booster = _fit(ds.x[list(tr_range)], ds.y[list(tr_range)], cfg)
    calibrator: Calibrator = fit_calibrator(
        _predict_raw(booster, ds.x[list(ca_range)]),
        ds.y[list(ca_range)],
        isotonic_min=t.isotonic_min,
    )
    class_counts = {
        "short": int(np.sum(ds.y == SHORT)),
        "flat": int(np.sum(ds.y == FLAT)),
        "long": int(np.sum(ds.y == LONG)),
    }
    meta: dict[str, Any] = {
        "version": version,
        "name": name,
        "asset": asset,
        "timeframe": bars.timeframe,
        "horizon": horizon_label(bars.timeframe, t.horizon_bars),
        "features": feature_names(cfg.features),
        "feature_params": cfg.features.model_dump(),
        "feature_set_version": feature_set_version(cfg.features),
        "barrier_k": t.barrier_k,
        "horizon_bars": t.horizon_bars,
        "train_range": [int(ds.as_of[0]), int(ds.as_of[-1])],
        "train_params": t.model_dump(),
        "calibration": calibrator.classes[0].method,
        "class_counts": class_counts,
        "min_confidence_for_metrics": min_confidence,
        "cost_rate": cost_rate,
        "metrics": metrics,
        "git_sha": _git_sha(),
        "libs": _lib_versions(),
    }
    path = artifacts_dir / version
    sha = write_artifact(
        path, model_text=booster.model_to_string(), calibrator=calibrator, meta=meta
    )
    return TrainResult(version=version, path=path, sha256=sha, meta=meta, metrics=metrics)
