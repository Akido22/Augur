"""Помощники ML-тестов: синтетические бары, маленький конфиг, обучение + промоушен."""

import sqlite3
from pathlib import Path

import numpy as np

from augur.config.settings import MLFeatureSettings, MLSettings, MLTrainSettings
from augur.fusion.ml.features import BarArrays

T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC
H = 3_600_000
DAY = 86_400_000


def synth_bars(n: int = 1400, seed: int = 7, start: int = T0) -> BarArrays:
    """Часовые бары: случайное блуждание с дрейфом, меняющим знак каждые 150 баров."""
    rng = np.random.default_rng(seed)
    drift = np.where((np.arange(n) // 150) % 2 == 0, 0.0015, -0.0015)
    c = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.006, n)))
    o = np.concatenate(([100.0], c[:-1]))
    hi = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.002, n)))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.002, n)))
    v = rng.uniform(1, 10, n)
    ts = start + np.arange(n, dtype=np.int64) * H
    return BarArrays("1h", ts, o, hi, lo, c, v)


def small_cfg(**train: object) -> MLSettings:
    t = {"train_bars": 600, "test_bars": 200, "n_estimators": 20, "calib_min": 100, **train}
    return MLSettings(
        features=MLFeatureSettings(window=120),
        train=MLTrainSettings(**t),  # type: ignore[arg-type]
    )


def insert_ohlcv(conn: sqlite3.Connection, bars: BarArrays, symbol: str, venue: str) -> None:
    conn.executemany(
        "INSERT INTO ohlcv(venue, symbol, timeframe, ts_open, open, high, low, close, volume, "
        "ingested_ts) VALUES (?,?,?,?,?,?,?,?,?,0)",
        [
            (
                venue,
                symbol,
                bars.timeframe,
                int(bars.ts_open[i]),
                float(bars.open[i]),
                float(bars.high[i]),
                float(bars.low[i]),
                float(bars.close[i]),
                float(bars.volume[i]),
            )
            for i in range(len(bars))
        ],
    )
    conn.commit()


class Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def train_register(
    conn: sqlite3.Connection,
    bars: BarArrays,
    cfg: MLSettings,
    tmp: Path,
    clock: Clock,
    *,
    asset: str = "BTC/USDT",
) -> str:
    from augur.backtest.costs import CostModel
    from augur.fusion.ml.registry import ModelRegistry, model_name
    from augur.fusion.ml.train import train_model

    reg = ModelRegistry(conn, clock=clock)
    version = reg.next_version()
    name = model_name(asset, bars.timeframe)
    res = train_model(
        bars,
        asset=asset,
        name=name,
        version=version,
        cfg=cfg,
        min_confidence=0.55,
        costs=CostModel(),
        artifacts_dir=tmp,
    )
    reg.register(name=name, version=version, artifact_path=res.path, trained_at=clock(), actor="t")
    return version


def promote_to_production(
    conn: sqlite3.Connection, version: str, cfg: MLSettings, clock: Clock
) -> None:
    from augur.config.settings import MLPromotionSettings
    from augur.fusion.ml.registry import ModelRegistry

    reg = ModelRegistry(conn, clock=clock)
    rules = MLPromotionSettings(max_ece=1.0, min_staging_days=cfg.promotion.min_staging_days)
    reg.promote(version, "staging", approved_by="tester", rules=rules)
    clock.now += (cfg.promotion.min_staging_days + 1) * DAY
    reg.promote(version, "production", approved_by="tester", rules=rules)
