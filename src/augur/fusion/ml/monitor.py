"""Nightly job ML и PSI-дрейф (ML_LLM_FUSION_SPEC.md §5.4, §6.4; 0.12.0).

На актив и production-модель `signal_clf_<asset>_<tf>`:

1. `ml_predictions.realized_return` — close-to-close доходность на горизонте модели для
   прогнозов, чей горизонт уже закрыт (только закрытые бары из `ohlcv`).
2. Качество за `drift.eval_window_days`: hit rate направленных прогнозов, accuracy и ECE по
   triple-barrier меткам с параметрами модели (`barrier_k`, `horizon_bars` из `meta.json`),
   P&L прогнозов с `confidence ≥ ml_min_confidence` за вычетом издержек общей `CostModel`.
   Это отчёт — решения по нему не принимаются.
3. PSI признаков: эталон — векторы `compute_features` (та же функция, что в обучении и
   инференсе) на барах диапазона обучения (`meta.train_range`), текущее окно — закрытые бары
   за последние `drift.window_bars` интервалов. Берутся `drift.top_features` важнейших
   признаков (gain). Дополнительно (только отчёт) — PSI распределения confidence: эталон —
   калибровочный хвост диапазона обучения (вне выборки бустера), текущее — векторы окна.
4. Решение пользователя (0.12.0): СРЕДНЕЕ PSI по топ-N > `drift.psi_threshold` →
   `ML_DRIFT_DETECTED` (задача на переобучение) и Fusion OPEN по активу
   (`augur.fusion.breaker`). PSI отдельных признаков (`drifted` — выше порога) и confidence
   в решение не входят: скользящие признаки автокоррелированы, правило «любой из топ-10»
   даёт ложные OPEN на стационарных данных. Отчёт — `ML_MONITOR_REPORT` в `audit_log` всегда.

Недостаточно данных для эталона или окна → статус `insufficient_data` (WARN, breaker не
трогается). Нет production-модели или она не загружается → `no_model`.
"""

import math
import sqlite3
from collections.abc import Callable
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict

from augur.config.settings import MLSettings
from augur.data.models import timeframe_ms
from augur.db.audit import append_audit
from augur.fusion.breaker import open_asset_breaker
from augur.fusion.ml.artifact import Predictor
from augur.fusion.ml.data import load_bar_arrays
from augur.fusion.ml.features import BarArrays, FeatureVector, compute_features, feature_set_version
from augur.fusion.ml.registry import ModelRecord, ModelRegistry, ModelUnavailable, model_name
from augur.fusion.ml.train import build_dataset

__all__ = [
    "DRIFT_TRIGGER",
    "MonitorReport",
    "fill_realized_returns",
    "psi",
    "run_monitor",
]

F64 = npt.NDArray[np.float64]
AUDIT_ACTOR = "ml_monitor"
DRIFT_TRIGGER = "ml_drift"
_EPS = 1e-4  # сглаживание пустых бинов PSI
_CONF_FEATURE = "__confidence__"

Status = Literal["ok", "drift", "insufficient_data", "no_model"]


class MonitorReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    asset: str
    timeframe: str
    model_version: str | None
    status: Status
    reason: str | None = None
    psi: dict[str, float | None] = {}  # признак → PSI (топ-N) и `__confidence__`; None — нет данных
    psi_mean: float | None = None  # среднее по топ-N признакам — решающая величина
    psi_max: float | None = None
    drifted: list[str] = []  # признаки с PSI выше порога (для разбора, не триггер)
    reference_n: int = 0
    current_n: int = 0
    realized_filled: int = 0
    quality: dict[str, float | int | None] = {}
    breaker_opened: bool = False


def psi(reference: F64, current: F64, bins: int) -> float:
    """Population Stability Index по квантильным бинам эталона: Σ (c − r)·ln(c / r)."""
    ref = reference[np.isfinite(reference)]
    cur = current[np.isfinite(current)]
    if ref.size == 0 or cur.size == 0:
        return float("nan")
    qs = np.quantile(ref, np.linspace(0.0, 1.0, bins + 1)[1:-1])
    edges = np.unique(qs)
    r = np.bincount(np.searchsorted(edges, ref, side="right"), minlength=edges.size + 1)
    c = np.bincount(np.searchsorted(edges, cur, side="right"), minlength=edges.size + 1)
    rp = np.clip(r / ref.size, _EPS, None)
    cp = np.clip(c / cur.size, _EPS, None)
    return float(np.sum((cp - rp) * np.log(cp / rp)))


def _horizon_ms(horizon: str) -> int:
    unit = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[horizon[-1]]
    return int(horizon[:-1]) * unit


def _close_at(conn: sqlite3.Connection, venue: str, asset: str, tf: str, close_ts: int) -> float:
    row = conn.execute(
        "SELECT close FROM ohlcv WHERE venue=? AND symbol=? AND timeframe=? AND ts_open=?",
        (venue, asset, tf, close_ts - timeframe_ms(tf)),
    ).fetchone()
    return float(row[0]) if row is not None and float(row[0]) > 0 else float("nan")


def fill_realized_returns(
    conn: sqlite3.Connection,
    rec: ModelRecord,
    *,
    asset: str,
    timeframe: str,
    venue: str,
    now_ms: int,
) -> int:
    """Заполняет `realized_return` прогнозов модели с закрытым горизонтом (без COMMIT)."""
    rows = conn.execute(
        "SELECT id, timestamp, horizon FROM ml_predictions WHERE model_id=? AND asset=? "
        "AND realized_return IS NULL ORDER BY timestamp",
        (rec.id, asset),
    ).fetchall()
    filled = 0
    for pid, ts, horizon in rows:
        end = int(ts) + _horizon_ms(str(horizon))
        if end > now_ms:
            break
        p0 = _close_at(conn, venue, asset, timeframe, int(ts))
        p1 = _close_at(conn, venue, asset, timeframe, end)
        if not (math.isfinite(p0) and math.isfinite(p1)):
            continue
        conn.execute(
            "UPDATE ml_predictions SET realized_return=? WHERE id=?",
            (round(p1 / p0 - 1.0, 10), pid),
        )
        filled += 1
    return filled


def _quality(
    conn: sqlite3.Connection,
    rec: ModelRecord,
    predictor: Predictor,
    *,
    asset: str,
    timeframe: str,
    cfg: MLSettings,
    now_ms: int,
    min_confidence: float,
    cost_rate: float,
) -> dict[str, float | int | None]:
    since = now_ms - cfg.drift.eval_window_days * 86_400_000
    preds = conn.execute(
        "SELECT timestamp, direction, confidence, realized_return FROM ml_predictions "
        "WHERE model_id=? AND asset=? AND timestamp >= ? AND realized_return IS NOT NULL "
        "ORDER BY timestamp",
        (rec.id, asset, since),
    ).fetchall()
    out: dict[str, float | int | None] = {"n": len(preds)}
    if not preds:
        return {**out, "hit_rate": None, "accuracy": None, "ece": None, "pnl_net": None}
    directional = [(d, r) for _, d, _, r in preds if d != "flat"]
    hits = sum(1 for d, r in directional if (r > 0 if d == "long" else r < 0))
    out["hit_rate"] = round(hits / len(directional), 6) if directional else None
    trades = [
        (1.0 if d == "long" else -1.0) * float(r) - 2 * cost_rate
        for _, d, c, r in preds
        if d != "flat" and float(c) >= min_confidence
    ]
    out["trades"] = len(trades)
    out["pnl_net"] = round(sum(trades), 8) if trades else 0.0
    # accuracy / ECE по triple-barrier меткам с параметрами модели (то, что модель учила)
    k = float(predictor.meta.get("barrier_k", cfg.train.barrier_k))
    h = int(predictor.meta.get("horizon_bars", cfg.train.horizon_bars))
    tf_ms = timeframe_ms(timeframe)
    first_ts = int(preds[0][0])
    bars = load_bar_arrays(
        conn,
        venue=cfg.venue,
        symbol=asset,
        timeframe=timeframe,
        since_open=first_ts - (cfg.features.window + 1) * tf_ms,
        until_close=now_ms,
    )
    labels: dict[int, int] = {}
    if bars is not None:
        lcfg = cfg.model_copy(
            update={"train": cfg.train.model_copy(update={"barrier_k": k, "horizon_bars": h})}
        )
        ds = build_dataset(bars, lcfg)
        labels = {int(t): int(y) for t, y in zip(ds.as_of, ds.y, strict=True)}
    names = ("short", "flat", "long")
    pairs = [
        (float(c), names.index(d) == labels[int(t)]) for t, d, c, _ in preds if int(t) in labels
    ]
    out["labelled"] = len(pairs)
    if not pairs:
        return {**out, "accuracy": None, "ece": None}
    conf = np.array([p[0] for p in pairs])
    ok = np.array([p[1] for p in pairs], dtype=np.float64)
    nb = cfg.drift.ece_bins
    ece = 0.0
    for b in range(nb):
        lo, hi = b / nb, (b + 1) / nb
        m = (conf > lo) & (conf <= hi) if b else (conf >= lo) & (conf <= hi)
        if m.any():
            ece += float(m.mean()) * abs(float(ok[m].mean()) - float(conf[m].mean()))
    out["accuracy"] = round(float(ok.mean()), 6)
    out["ece"] = round(ece, 6)
    return out


def _vectors(bars: BarArrays | None, as_ofs: list[int], cfg: MLSettings) -> list[FeatureVector]:
    if bars is None:
        return []
    out = []
    for t in as_ofs:
        fv = compute_features(bars, t, cfg.features)
        if fv is not None:
            out.append(fv)
    return out


def _thin(values: list[int], limit: int) -> list[int]:
    if len(values) <= limit:
        return values
    idx = np.linspace(0, len(values) - 1, limit).round().astype(np.int64)
    return [values[i] for i in sorted(set(idx.tolist()))]


def _drift(
    conn: sqlite3.Connection,
    predictor: Predictor,
    *,
    asset: str,
    timeframe: str,
    cfg: MLSettings,
    now_ms: int,
) -> tuple[dict[str, float], int, int, str | None]:
    """(psi по признакам и confidence, n эталона, n окна, причина нехватки данных)."""
    d = cfg.drift
    tf_ms = timeframe_ms(timeframe)
    warm = (cfg.features.window + 1) * tf_ms
    train_range = predictor.meta.get("train_range")
    if not (isinstance(train_range, list) and len(train_range) == 2):
        return {}, 0, 0, "model meta has no train_range"
    lo, hi = int(train_range[0]), int(train_range[1])
    ref_bars = load_bar_arrays(
        conn,
        venue=cfg.venue,
        symbol=asset,
        timeframe=timeframe,
        since_open=lo - warm,
        until_close=hi,
    )
    ref_times = (
        [ref_bars.close_time(i) for i in range(len(ref_bars)) if lo <= ref_bars.close_time(i) <= hi]
        if ref_bars is not None
        else []
    )
    ref = _vectors(ref_bars, _thin(ref_times, d.reference_max), cfg)
    since = now_ms - d.window_bars * tf_ms
    cur_bars = load_bar_arrays(
        conn,
        venue=cfg.venue,
        symbol=asset,
        timeframe=timeframe,
        until_close=now_ms,
        since_open=since - warm,
    )
    cur_times = (
        [t for t in (cur_bars.close_time(i) for i in range(len(cur_bars))) if t > since]
        if cur_bars is not None
        else []
    )
    cur = _vectors(cur_bars, cur_times, cfg)
    if len(ref) < d.min_reference:
        return {}, len(ref), len(cur), f"reference vectors {len(ref)} < {d.min_reference}"
    if len(cur) < d.window_bars // 2:
        return {}, len(ref), len(cur), f"current vectors {len(cur)} < {d.window_bars // 2}"
    names = list(predictor.feature_names)
    imp = predictor.feature_importance()
    top = sorted(names, key=lambda n: (-imp.get(n, 0.0), names.index(n)))[: d.top_features]
    xr = np.array([fv.as_list() for fv in ref], dtype=np.float64)
    xc = np.array([fv.as_list() for fv in cur], dtype=np.float64)
    out = {n: round(psi(xr[:, names.index(n)], xc[:, names.index(n)], d.bins), 6) for n in top}
    if d.check_confidence:
        frac = float(predictor.meta.get("train_params", {}).get("calib_fraction", 0.15))
        tail = ref[-max(d.bins * 2, int(len(ref) * frac)) :]
        conf_ref = predictor.predict_proba(tail).max(axis=1)
        conf_cur = predictor.predict_proba(cur).max(axis=1)
        out[_CONF_FEATURE] = round(psi(conf_ref, conf_cur, d.bins), 6)
    return out, len(ref), len(cur), None


def run_monitor(
    conn: sqlite3.Connection,
    cfg: MLSettings,
    *,
    asset: str,
    timeframe: str,
    clock: Callable[[], int],
    min_confidence: float,
    cost_rate: float,
    registry: ModelRegistry | None = None,
) -> MonitorReport:
    """Nightly job по одному активу: realized_return → качество → PSI → аудит / breaker."""
    now = clock()
    reg = registry or ModelRegistry(conn, clock=clock)
    name = model_name(asset, timeframe)
    rec = reg.get_production(name)
    predictor: Predictor | None = None
    reason: str | None = None
    if rec is None:
        reason = f"no production model {name}"
    else:
        try:
            predictor = reg.load(rec, feature_set_version=feature_set_version(cfg.features))
        except ModelUnavailable as e:
            reason = str(e)
    if rec is None or predictor is None:
        report = MonitorReport(
            asset=asset,
            timeframe=timeframe,
            model_version=rec.version if rec else None,
            status="no_model",
            reason=reason,
        )
        return _finish(conn, report, now)
    try:
        filled = fill_realized_returns(
            conn, rec, asset=asset, timeframe=timeframe, venue=cfg.venue, now_ms=now
        )
        quality = _quality(
            conn,
            rec,
            predictor,
            asset=asset,
            timeframe=timeframe,
            cfg=cfg,
            now_ms=now,
            min_confidence=min_confidence,
            cost_rate=cost_rate,
        )
        psis, n_ref, n_cur, short = _drift(
            conn, predictor, asset=asset, timeframe=timeframe, cfg=cfg, now_ms=now
        )
    except BaseException:
        conn.rollback()
        raise
    thr = cfg.drift.psi_threshold
    feats = {k: v for k, v in psis.items() if k != _CONF_FEATURE and math.isfinite(v)}
    psi_out: dict[str, float | None] = {
        k: (v if math.isfinite(v) else None) for k, v in psis.items()
    }
    drifted = sorted(k for k, v in feats.items() if v > thr)
    psi_mean = round(sum(feats.values()) / len(feats), 6) if feats else None
    if short:
        status: Status = "insufficient_data"
    elif psi_mean is None:
        status, short = "insufficient_data", "no finite PSI values"
    else:
        status = "drift" if psi_mean > thr else "ok"
    report = MonitorReport(
        asset=asset,
        timeframe=timeframe,
        model_version=rec.version,
        status=status,
        reason=short,
        psi=psi_out,
        psi_mean=psi_mean,
        psi_max=max(feats.values()) if feats else None,
        drifted=drifted,
        reference_n=n_ref,
        current_n=n_cur,
        realized_filled=filled,
        quality=quality,
    )
    return _finish(conn, report, now)


def _finish(conn: sqlite3.Connection, report: MonitorReport, now: int) -> MonitorReport:
    payload: dict[str, Any] = report.model_dump(exclude={"breaker_opened"})
    try:
        if report.status == "drift":
            append_audit(
                conn,
                actor=AUDIT_ACTOR,
                event="ML_DRIFT_DETECTED",
                severity="WARN",
                ref_type="ml_model",
                ref_id=report.model_version,
                payload={
                    "asset": report.asset,
                    "model_version": report.model_version,
                    "drifted": report.drifted,
                    "psi_mean": report.psi_mean,
                    "psi_threshold_rule": "mean_top_features",
                    "retrain_required": True,
                },
                ts=now,
            )
            opened = open_asset_breaker(
                conn,
                report.asset,
                trigger=DRIFT_TRIGGER,
                detail={
                    "model_version": report.model_version,
                    "drifted": report.drifted,
                    "psi_mean": report.psi_mean,
                },
                actor=AUDIT_ACTOR,
                ts=now,
            )
            report = report.model_copy(update={"breaker_opened": opened})
        append_audit(
            conn,
            actor=AUDIT_ACTOR,
            event="ML_MONITOR_REPORT",
            severity="INFO" if report.status in ("ok", "drift") else "WARN",
            ref_type="fusion_asset",
            ref_id=report.asset,
            payload={**payload, "breaker_opened": report.breaker_opened},
            ts=now,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return report
