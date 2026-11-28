"""Признаки без look-ahead, triple-barrier, walk-forward, калибровка, режим (FUSION SPEC §2)."""

import numpy as np
import pytest

from augur.config.settings import MLFeatureSettings, MLRegimeSettings
from augur.fusion.ml.calibration import Calibrator, fit_calibrator
from augur.fusion.ml.features import (
    BarArrays,
    FeatureVector,
    compute_features,
    feature_names,
    feature_set_version,
)
from augur.fusion.ml.labels import FLAT, LONG, SHORT, triple_barrier
from augur.fusion.ml.regime import regime_tag
from augur.fusion.ml.walk_forward import purged_walk_forward
from tests.fusion._ml import synth_bars

CFG = MLFeatureSettings(window=120)


def _prefix(b: BarArrays, n: int) -> BarArrays:
    return BarArrays(
        b.timeframe, b.ts_open[:n], b.open[:n], b.high[:n], b.low[:n], b.close[:n], b.volume[:n]
    )


def test_future_bars_do_not_change_features() -> None:
    bars = synth_bars(600)
    for i in (150, 300, 450):
        as_of = bars.close_time(i)
        base = compute_features(_prefix(bars, i + 1), as_of, CFG)
        with_future = compute_features(bars, as_of, CFG)
        assert base is not None and base == with_future  # бит-в-бит
        assert base.input_hash() == with_future.input_hash()  # type: ignore[union-attr]


def test_unclosed_bar_is_ignored() -> None:
    bars = synth_bars(300)
    as_of = bars.close_time(200)
    fv_closed = compute_features(bars, as_of, CFG)
    fv_mid_bar = compute_features(bars, as_of + 1_000, CFG)  # бар 201 ещё не закрыт
    assert fv_closed == fv_mid_bar


def test_not_enough_history_returns_none() -> None:
    bars = synth_bars(300)
    assert compute_features(bars, bars.close_time(CFG.window - 2), CFG) is None
    assert compute_features(bars, bars.close_time(CFG.window - 1), CFG) is not None


def test_vector_is_finite_ordered_and_versioned() -> None:
    bars = synth_bars(300)
    fv = compute_features(bars, bars.close_time(250), CFG)
    assert isinstance(fv, FeatureVector)
    assert list(fv.values) == feature_names(CFG)
    assert all(np.isfinite(v) for v in fv.values.values())
    assert fv.feature_set_version == feature_set_version(CFG)
    assert fv.feature_set_version.startswith("fs_") and len(fv.feature_set_version) == 15
    assert feature_set_version(MLFeatureSettings(window=121)) != fv.feature_set_version
    assert not any("close" == k or "open" == k for k in fv.values)  # сырые цены не подаются


def _arr(x: list[float]) -> np.ndarray:
    return np.array(x, dtype=np.float64)


@pytest.mark.parametrize(
    ("highs", "lows", "expected"),
    [
        ([101, 104], [99, 99], LONG),  # верхний барьер на втором баре
        ([101, 101], [99, 96], SHORT),  # нижний барьер
        ([104, 101], [96, 99], FLAT),  # оба в одном баре → flat
        ([101, 101], [99, 99], FLAT),  # вертикальный барьер
        ([101, 104], [95, 99], SHORT),  # первое касание — нижний
    ],
)
def test_triple_barrier_golden(highs: list[float], lows: list[float], expected: int) -> None:
    close = _arr([100, 100, 100])
    high = _arr([100, *highs])
    low = _arr([100, *lows])
    atr = _arr([2, 2, 2])  # k=1.5 → барьеры 103 / 97
    labels = triple_barrier(high, low, close, atr, k=1.5, h=2)
    assert labels[0] == expected
    assert labels[1:] == [None, None]  # последние h баров не размечаются


def test_walk_forward_no_overlap_purge_embargo() -> None:
    n, h = 3000, 4
    folds = list(
        purged_walk_forward(n, train=1000, test=300, horizon=h, calib_fraction=0.15, calib_min=100)
    )
    assert len(folds) >= 3
    prev_test_end = -1
    for f in folds:
        assert f.train.stop + h <= f.calib.start  # purge train → calib
        assert f.calib.stop + h <= f.test.start  # purge calib → test
        assert not set(f.train) & set(f.test) and not set(f.calib) & set(f.test)
        if prev_test_end >= 0:
            assert f.test.start - prev_test_end >= max(h, n // 100)  # embargo
        prev_test_end = f.test.stop


def test_calibrator_isotonic_and_platt_roundtrip() -> None:
    rng = np.random.default_rng(0)
    raw = rng.dirichlet([2, 2, 2], size=1500)
    labels = np.array([rng.choice(3, p=p) for p in raw], dtype=np.int64)
    iso = fit_calibrator(raw, labels, isotonic_min=1000)
    platt = fit_calibrator(raw[:200], labels[:200], isotonic_min=1000)
    assert iso.classes[0].method == "isotonic" and platt.classes[0].method == "platt"
    for cal in (iso, platt):
        out = cal.apply(raw)
        assert np.allclose(out.sum(axis=1), 1.0) and (out > 0).all()
        again = Calibrator.model_validate_json(cal.model_dump_json())  # JSON, не pickle
        assert np.array_equal(again.apply(raw), out)
    xs = np.linspace(0, 1, 50)
    ys = iso.classes[0].apply(xs)
    assert (np.diff(ys) >= -1e-12).all()  # isotonic монотонен


def _fv(**vals: float) -> FeatureVector:
    base = {"adx": 0.1, "rv_ratio": 1.0, "atr_pct_rank": 0.5, "di_diff": 0.0}
    return FeatureVector(as_of=0, values={**base, **vals}, feature_set_version="fs_x")


@pytest.mark.parametrize(
    ("vals", "tag"),
    [
        ({"rv_ratio": 3.0}, "chaotic"),
        ({"atr_pct_rank": 0.95}, "high_vol"),
        ({"adx": 0.3, "di_diff": 0.1, "rv_ratio": 3.0}, "trend_up"),  # тренд — не chaotic
        ({"adx": 0.3, "di_diff": -0.1}, "trend_down"),
        ({}, "range"),
    ],
)
def test_regime_rules(vals: dict[str, float], tag: str) -> None:
    assert regime_tag(_fv(**vals), MLRegimeSettings()) == tag
