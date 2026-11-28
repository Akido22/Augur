"""Детерминированный `regime_tag` по правилам (skill ml-training-inference п. 12; SPEC §2.1).

Порядок: `chaotic` → `high_vol` → тренд → `range`. Пороги — `MLRegimeSettings`, модель не
используется (HMM — после 0.10.0).
"""

from typing import Literal

from augur.config.settings import MLRegimeSettings
from augur.fusion.ml.features import FeatureVector

__all__ = ["RegimeTag", "regime_tag"]

RegimeTag = Literal["trend_up", "trend_down", "range", "high_vol", "chaotic"]


def regime_tag(fv: FeatureVector, cfg: MLRegimeSettings) -> RegimeTag:
    v = fv.values
    adx = v["adx"] * 100.0
    trending = adx >= cfg.trend_adx
    if v["rv_ratio"] >= cfg.chaotic_rv_ratio and not trending:
        return "chaotic"
    if v["atr_pct_rank"] >= cfg.high_vol_atr_pct and not trending:
        return "high_vol"
    if trending:
        return "trend_up" if v["di_diff"] > 0 else "trend_down"
    return "range"
