"""Единственная функция признаков для обучения и инференса (ML_LLM_FUSION_SPEC.md §2.2).

- В расчёт идут только закрытые бары: `ts_open + timeframe_ms <= as_of`.
- Вектор зависит только от хвоста из `cfg.window` закрытых баров (одинаковый warm-up при
  обучении и инференсе). Меньше истории → `None` («ML отсутствует»), а не NaN-вектор.
- Только стационарные преобразования (log-returns, отношения, z-score); сырые цены не подаются.
- Индикаторы — numpy, без TA-Lib (skill ml-training-inference, «Объём 0.10.0»).
"""

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict

from augur.config.settings import MLFeatureSettings
from augur.data.models import Bar, timeframe_ms

__all__ = [
    "FEATURES_CODE_VERSION",
    "BarArrays",
    "FeatureVector",
    "compute_features",
    "feature_names",
    "feature_set_version",
]

# Версия кода признаков: менять при любом изменении формул ниже (входит в feature_set_version).
FEATURES_CODE_VERSION = "features@1"

F64 = npt.NDArray[np.float64]
_RSI_N = 14
_ATR_N = 14
_ADX_N = 14
_BB_N = 20
_VOL_N = 20
_RV_SHORT = 20
_RV_LONG = 100
_STOCH_N = 14
_ATR_RANK_N = 100

_BASE_FEATURES = (
    "range_rel",
    "close_pos",
    "vol_z",
    "rsi",
    "atr_rel",
    "atr_pct_rank",
    "macd_hist_rel",
    "adx",
    "di_diff",
    "bb_pctb",
    "bb_width",
    "stoch_k",
    "rv20",
    "rv_ratio",
    "ret_mean_20",
    "ret_lag1",
    "ret_lag2",
    "ret_lag3",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
)


@dataclass(frozen=True)
class BarArrays:
    """Колонки OHLCV одного инструмента и таймфрейма, по возрастанию `ts_open` (Unix ms UTC)."""

    timeframe: str
    ts_open: npt.NDArray[np.int64]
    open: F64
    high: F64
    low: F64
    close: F64
    volume: F64

    @classmethod
    def from_bars(cls, bars: Sequence[Bar]) -> "BarArrays":
        if not bars:
            raise ValueError("bars must not be empty")
        tf = bars[0].timeframe
        if any(b.timeframe != tf for b in bars):
            raise ValueError("bars must share one timeframe")
        ts = np.array([b.ts_open for b in bars], dtype=np.int64)
        if np.any(np.diff(ts) <= 0):
            raise ValueError("bars must be strictly increasing by ts_open")
        return cls(
            timeframe=tf,
            ts_open=ts,
            open=np.array([b.open for b in bars], dtype=np.float64),
            high=np.array([b.high for b in bars], dtype=np.float64),
            low=np.array([b.low for b in bars], dtype=np.float64),
            close=np.array([b.close for b in bars], dtype=np.float64),
            volume=np.array([b.volume for b in bars], dtype=np.float64),
        )

    def __len__(self) -> int:
        return int(self.ts_open.shape[0])

    def close_time(self, i: int) -> int:
        return int(self.ts_open[i]) + timeframe_ms(self.timeframe)

    def closed_count(self, as_of: int) -> int:
        """Число баров с `close_time <= as_of` (префикс массива)."""
        cutoff = as_of - timeframe_ms(self.timeframe)
        return int(np.searchsorted(self.ts_open, cutoff, side="right"))


class FeatureVector(BaseModel):
    """Вектор признаков на `as_of` (close_time последнего бара окна)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: int
    values: dict[str, float]
    feature_set_version: str

    def as_list(self) -> list[float]:
        return list(self.values.values())

    def input_hash(self) -> str:
        """`sha256:` канонического вектора (FUSION SPEC §2.4): имена + значения через repr."""
        canon = json.dumps([[k, repr(v)] for k, v in self.values.items()], separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


def feature_names(cfg: MLFeatureSettings) -> list[str]:
    return [f"ret_{n}" for n in cfg.return_lags] + list(_BASE_FEATURES)


def feature_set_version(cfg: MLFeatureSettings) -> str:
    """`fs_` + sha256(список фич + параметры + версия кода)[:12] (FUSION SPEC §2.2)."""
    src = json.dumps(
        {"names": feature_names(cfg), "params": cfg.model_dump(), "code": FEATURES_CODE_VERSION},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "fs_" + hashlib.sha256(src.encode("utf-8")).hexdigest()[:12]


# --- индикаторы (только назад по времени) --------------------------------------------


def _wilder(x: F64, n: int) -> F64:
    """Сглаживание Уайлдера; первые n−1 значений — NaN, затем SMA(n) и рекурсия."""
    out = np.full_like(x, np.nan)
    if x.shape[0] < n:
        return out
    acc = float(np.mean(x[:n]))
    out[n - 1] = acc
    for i in range(n, x.shape[0]):
        acc = acc + (float(x[i]) - acc) / n
        out[i] = acc
    return out


def _ema(x: F64, n: int) -> F64:
    out = np.empty_like(x)
    alpha = 2.0 / (n + 1)
    acc = float(x[0])
    for i in range(x.shape[0]):
        acc = acc + alpha * (float(x[i]) - acc)
        out[i] = acc
    return out


def _true_range(h: F64, low: F64, c: F64) -> F64:
    prev_c = np.concatenate(([c[0]], c[:-1]))
    out: F64 = np.maximum(np.maximum(h - low, np.abs(h - prev_c)), np.abs(low - prev_c))
    return out


def _safe_div(a: float, b: float) -> float:
    return a / b if b != 0 and math.isfinite(b) else 0.0


def _std(x: F64) -> float:
    return float(np.std(x)) if x.shape[0] > 1 else 0.0


def compute_features(bars: BarArrays, as_of: int, cfg: MLFeatureSettings) -> FeatureVector | None:
    """Признаки на `as_of` (Unix ms UTC) по закрытым барам. None — истории не хватает."""
    end = bars.closed_count(as_of)
    w = cfg.window
    if end < w or w < max(cfg.return_lags, default=1) + 2 or w < _RV_LONG + 1:
        return None
    sl = slice(end - w, end)
    h, low, c, v = bars.high[sl], bars.low[sl], bars.close[sl], bars.volume[sl]
    if np.any(c <= 0) or np.any(h < low):
        return None
    logc = np.log(c)
    r = np.diff(logc)
    vals: dict[str, float] = {f"ret_{n}": float(logc[-1] - logc[-1 - n]) for n in cfg.return_lags}

    rng = float(h[-1] - low[-1])
    vals["range_rel"] = rng / float(c[-1])
    vals["close_pos"] = _safe_div(float(c[-1] - low[-1]), rng) if rng > 0 else 0.5
    vtail = v[-_VOL_N:]
    vals["vol_z"] = _safe_div(float(v[-1] - np.mean(vtail)), _std(vtail))

    d = np.diff(c)
    gain = _wilder(np.maximum(d, 0.0), _RSI_N)
    loss = _wilder(np.maximum(-d, 0.0), _RSI_N)
    g, lo_ = float(gain[-1]), float(loss[-1])
    vals["rsi"] = 1.0 if lo_ == 0 and g > 0 else (0.5 if lo_ == 0 else 1.0 - 1.0 / (1.0 + g / lo_))

    tr = _true_range(h, low, c)
    atr = _wilder(tr, _ATR_N)
    vals["atr_rel"] = float(atr[-1]) / float(c[-1])
    atr_tail = atr[-_ATR_RANK_N:]
    vals["atr_pct_rank"] = float(np.mean(atr_tail <= atr[-1]))

    macd = _ema(c, 12) - _ema(c, 26)
    hist = macd - _ema(macd, 9)
    vals["macd_hist_rel"] = float(hist[-1]) / float(c[-1])

    up = np.diff(h)
    down = -np.diff(low)
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    atr_dm = _wilder(tr[1:], _ADX_N)
    pdi = 100.0 * _wilder(plus_dm, _ADX_N) / np.where(atr_dm > 0, atr_dm, np.nan)
    mdi = 100.0 * _wilder(minus_dm, _ADX_N) / np.where(atr_dm > 0, atr_dm, np.nan)
    den = pdi + mdi
    dx = 100.0 * np.abs(pdi - mdi) / np.where(den > 0, den, np.nan)
    dx_valid = dx[~np.isnan(dx)]
    adx = _wilder(dx_valid, _ADX_N) if dx_valid.shape[0] >= _ADX_N else np.array([np.nan])
    vals["adx"] = float(adx[-1]) / 100.0 if math.isfinite(float(adx[-1])) else 0.0
    di = float(pdi[-1] - mdi[-1])
    vals["di_diff"] = di / 100.0 if math.isfinite(di) else 0.0

    bb = c[-_BB_N:]
    mid, sd = float(np.mean(bb)), _std(bb)
    upper, lower = mid + 2 * sd, mid - 2 * sd
    vals["bb_pctb"] = _safe_div(float(c[-1]) - lower, upper - lower) if sd > 0 else 0.5
    vals["bb_width"] = (upper - lower) / mid

    hh, ll = float(np.max(h[-_STOCH_N:])), float(np.min(low[-_STOCH_N:]))
    vals["stoch_k"] = _safe_div(float(c[-1]) - ll, hh - ll) if hh > ll else 0.5

    rv20 = _std(r[-_RV_SHORT:])
    rv100 = _std(r[-_RV_LONG:])
    vals["rv20"] = rv20
    vals["rv_ratio"] = _safe_div(rv20, rv100) if rv100 > 0 else 1.0
    vals["ret_mean_20"] = float(np.mean(r[-_RV_SHORT:]))
    vals["ret_lag1"] = float(r[-2])
    vals["ret_lag2"] = float(r[-3])
    vals["ret_lag3"] = float(r[-4])

    close_ts = int(bars.ts_open[end - 1]) + timeframe_ms(bars.timeframe)
    hour = (close_ts // 3_600_000) % 24
    dow = ((close_ts // 86_400_000) + 3) % 7  # 1970-01-01 — четверг → пн=0
    vals["hour_sin"] = math.sin(2 * math.pi * hour / 24)
    vals["hour_cos"] = math.cos(2 * math.pi * hour / 24)
    vals["dow_sin"] = math.sin(2 * math.pi * dow / 7)
    vals["dow_cos"] = math.cos(2 * math.pi * dow / 7)

    ordered = {name: float(vals[name]) for name in feature_names(cfg)}
    if not all(math.isfinite(x) for x in ordered.values()):
        return None
    return FeatureVector(
        as_of=close_ts, values=ordered, feature_set_version=feature_set_version(cfg)
    )
