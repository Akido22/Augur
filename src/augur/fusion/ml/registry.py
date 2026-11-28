"""Реестр моделей (ML_LLM_FUSION_SPEC.md §2.4; skill ml-training-inference, п. 9–10).

- `ml_model_version = lightgbm_v<N>`, `N` монотонен глобально; версия пишется в
  `artifacts(kind='ml_model')` и `ml_models.version`.
- Новая модель — всегда `candidate`. Автопромоушена нет: `promote()` вызывается человеком
  (CLI с `--confirm`), каждая смена статуса — `ML_MODEL_STATUS` в `audit_log`.
- Инференс — только `get_production(name)` → `load(rec)`: сверка sha256 и
  `feature_set_version`; несовпадение → `ModelUnavailable`.
"""

import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from augur.config.settings import MLPromotionSettings
from augur.db.audit import append_audit
from augur.fusion.ml.artifact import ArtifactError, Predictor, artifact_sha256, read_artifact

__all__ = [
    "AUDIT_ACTOR",
    "REQUIRED_METRICS",
    "ModelRecord",
    "ModelRegistry",
    "ModelUnavailable",
    "PromotionError",
    "RegistrationError",
    "Status",
    "model_name",
]

AUDIT_ACTOR = "ml_registry"
REQUIRED_METRICS = ("logloss", "brier", "ece", "sharpe", "max_dd")
Status = Literal["candidate", "staging", "production", "retired"]
_VERSION_RE = re.compile(r"^lightgbm_v(\d+)$")
_DAY_MS = 86_400_000


class ModelUnavailable(RuntimeError):
    """Модель нельзя использовать для инференса (нет, подменена, другие признаки)."""


class RegistrationError(ValueError):
    """Модель не может быть зарегистрирована (нет калибратора/метрик, неверная версия)."""


class PromotionError(ValueError):
    """Переход статуса запрещён правилами §2.4."""


def model_name(asset: str, timeframe: str) -> str:
    """`signal_clf_<asset>_<tf>`: BTC/USDT, 1h → signal_clf_btcusdt_1h."""
    return f"signal_clf_{re.sub(r'[^a-z0-9]', '', asset.lower())}_{timeframe}"


class ModelRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    name: str
    version: str
    artifact_path: str
    artifact_sha256: str
    feature_set_version: str
    trained_at: int
    train_from: int | None
    train_to: int | None
    metrics: dict[str, Any]
    status: Status
    approved_by: str | None
    status_changed_at: int


_COLS = (
    "id, name, version, artifact_path, artifact_sha256, feature_set_version, trained_at, "
    "train_from, train_to, metrics_json, status, approved_by, status_changed_at"
)


def _row(r: tuple[Any, ...]) -> ModelRecord:
    return ModelRecord(
        id=r[0],
        name=r[1],
        version=r[2],
        artifact_path=r[3],
        artifact_sha256=r[4],
        feature_set_version=r[5],
        trained_at=r[6],
        train_from=r[7],
        train_to=r[8],
        metrics=json.loads(r[9]),
        status=r[10],
        approved_by=r[11],
        status_changed_at=r[12],
    )


class ModelRegistry:
    def __init__(self, conn: sqlite3.Connection, *, clock: Callable[[], int]) -> None:
        self._conn = conn
        self._clock = clock

    # --- версии ----------------------------------------------------------------------

    def next_version(self) -> str:
        """Следующая `lightgbm_v<N>`: максимум по `artifacts` и `ml_models` + 1."""
        rows = self._conn.execute(
            "SELECT version FROM artifacts WHERE kind='ml_model' "
            "UNION SELECT version FROM ml_models"
        ).fetchall()
        nums = [int(m.group(1)) for (v,) in rows if (m := _VERSION_RE.match(v))]
        return f"lightgbm_v{max(nums, default=0) + 1}"

    # --- регистрация -----------------------------------------------------------------

    def register(
        self,
        *,
        name: str,
        version: str,
        artifact_path: Path,
        trained_at: int,
        actor: str,
    ) -> ModelRecord:
        """Регистрирует артефакт как `candidate` (одна транзакция с audit)."""
        if not _VERSION_RE.match(version) or version != self.next_version():
            raise RegistrationError(f"version must be {self.next_version()}, got {version}")
        if not (artifact_path / "calibrator.json").is_file():
            raise RegistrationError("calibrator is required (FUSION SPEC §2.6)")
        try:
            meta = json.loads((artifact_path / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise RegistrationError(f"meta.json unreadable: {e}") from e
        metrics = meta.get("metrics") or {}
        missing = [m for m in REQUIRED_METRICS if m not in metrics]
        if missing:
            raise RegistrationError(f"metrics missing: {missing}")
        if meta.get("version") != version:
            raise RegistrationError("meta.json version does not match")
        sha = artifact_sha256(artifact_path)
        now = self._clock()
        train_from, train_to = meta.get("train_range", [None, None])
        path_s = artifact_path.as_posix()
        try:
            self._conn.execute(
                "INSERT INTO artifacts(kind, version, sha256, path, metadata, created_at) "
                "VALUES ('ml_model', ?, ?, ?, ?, ?)",
                (version, sha, path_s, json.dumps(meta, sort_keys=True), now),
            )
            cur = self._conn.execute(
                "INSERT INTO ml_models(name, version, artifact_path, artifact_sha256, "
                "feature_set_version, trained_at, train_from, train_to, metrics_json, status, "
                "status_changed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?)",
                (
                    name,
                    version,
                    path_s,
                    sha,
                    meta["feature_set_version"],
                    trained_at,
                    train_from,
                    train_to,
                    json.dumps(metrics, sort_keys=True),
                    now,
                ),
            )
            model_id = int(cur.lastrowid or 0)
            append_audit(
                self._conn,
                actor=AUDIT_ACTOR,
                event="ML_MODEL_REGISTERED",
                severity="INFO",
                ref_type="ml_model",
                ref_id=version,
                payload={"name": name, "sha256": sha, "metrics": metrics, "by": actor},
                ts=now,
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return self.get(model_id)

    # --- чтение ----------------------------------------------------------------------

    def get(self, model_id: int) -> ModelRecord:
        r = self._conn.execute(f"SELECT {_COLS} FROM ml_models WHERE id=?", (model_id,)).fetchone()  # noqa: S608 — константа
        if r is None:
            raise KeyError(model_id)
        return _row(r)

    def by_version(self, version: str) -> ModelRecord:
        r = self._conn.execute(
            f"SELECT {_COLS} FROM ml_models WHERE version=?",  # noqa: S608 — константа
            (version,),
        ).fetchone()
        if r is None:
            raise KeyError(version)
        return _row(r)

    def list(self) -> list[ModelRecord]:
        rows = self._conn.execute(f"SELECT {_COLS} FROM ml_models ORDER BY id").fetchall()  # noqa: S608
        return [_row(r) for r in rows]

    def get_production(self, name: str) -> ModelRecord | None:
        r = self._conn.execute(
            f"SELECT {_COLS} FROM ml_models WHERE name=? AND status='production'",  # noqa: S608
            (name,),
        ).fetchone()
        return _row(r) if r else None

    # --- промоушен -------------------------------------------------------------------

    def promote(
        self, version: str, to: Status, *, approved_by: str, rules: MLPromotionSettings
    ) -> ModelRecord:
        """Смена статуса по §2.4. production: ≥ N дней в staging, ECE ≤ порога, Sharpe не хуже."""
        if not approved_by.strip():
            raise PromotionError("approved_by is required")
        rec = self.by_version(version)
        now = self._clock()
        allowed: dict[Status, set[Status]] = {
            "candidate": {"staging", "retired"},
            "staging": {"production", "retired"},
            "production": {"retired"},
            "retired": set(),
        }
        if to not in allowed[rec.status]:
            raise PromotionError(f"{rec.status} → {to} is not allowed")
        retired_prev: ModelRecord | None = None
        if to == "production":
            days = (now - rec.status_changed_at) / _DAY_MS
            if days < rules.min_staging_days:
                raise PromotionError(
                    f"staging for {days:.1f} days < {rules.min_staging_days} (FUSION SPEC §2.4)"
                )
            if float(rec.metrics["ece"]) > rules.max_ece:
                raise PromotionError(f"ECE {rec.metrics['ece']} > {rules.max_ece}")
            retired_prev = self.get_production(rec.name)
            if retired_prev is not None and float(rec.metrics["sharpe"]) < float(
                retired_prev.metrics["sharpe"]
            ):
                raise PromotionError(
                    f"sharpe {rec.metrics['sharpe']} < production {retired_prev.metrics['sharpe']}"
                )
        try:
            if retired_prev is not None:
                self._set_status(retired_prev, "retired", approved_by, now)
            self._set_status(rec, to, approved_by, now)
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return self.by_version(version)

    def _set_status(self, rec: ModelRecord, to: Status, actor: str, now: int) -> None:
        self._conn.execute(
            "UPDATE ml_models SET status=?, approved_by=?, status_changed_at=? WHERE id=?",
            (to, actor, now, rec.id),
        )
        append_audit(
            self._conn,
            actor=AUDIT_ACTOR,
            event="ML_MODEL_STATUS",
            severity="WARN" if to == "production" else "INFO",
            ref_type="ml_model",
            ref_id=rec.version,
            payload={"name": rec.name, "from": rec.status, "to": to, "approved_by": actor},
            ts=now,
        )

    # --- загрузка --------------------------------------------------------------------

    def load(self, rec: ModelRecord, *, feature_set_version: str) -> Predictor:
        """Загрузка со сверкой sha256 и признаков. Любое расхождение → `ModelUnavailable`."""
        path = Path(rec.artifact_path)
        try:
            sha = artifact_sha256(path)
        except ArtifactError as e:
            raise ModelUnavailable(str(e)) from e
        if sha != rec.artifact_sha256:
            raise ModelUnavailable(f"sha256 mismatch for {rec.version}")
        if rec.feature_set_version != feature_set_version:
            raise ModelUnavailable(
                f"feature_set_version {rec.feature_set_version} != code {feature_set_version}"
            )
        try:
            predictor = read_artifact(path)
        except ArtifactError as e:
            raise ModelUnavailable(str(e)) from e
        if predictor.meta.get("feature_set_version") != feature_set_version:
            raise ModelUnavailable("meta.json feature_set_version mismatch")
        return predictor
