"""Артефакт модели `artifacts/ml/lightgbm_v<N>/` (VERSIONING.md §2.3; FUSION SPEC §2.4, §2.6).

Файлы: `model.txt` (нативный формат LightGBM), `calibrator.json`, `meta.json`. Pickle/joblib
запрещены. `sha256` — по отсортированным именам файлов и их содержимому. Артефакт неизменяем:
запись в существующий каталог запрещена.
"""

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt

from augur.fusion.ml.calibration import Calibrator
from augur.fusion.ml.features import FeatureVector

if TYPE_CHECKING:
    import lightgbm

__all__ = [
    "ARTIFACT_FILES",
    "ArtifactError",
    "Predictor",
    "artifact_sha256",
    "read_artifact",
    "write_artifact",
]

ARTIFACT_FILES = ("calibrator.json", "meta.json", "model.txt")


class ArtifactError(RuntimeError):
    """Артефакт отсутствует, неполон или уже существует."""


def _dump(obj: Any) -> bytes:
    return (json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def artifact_sha256(path: Path) -> str:
    """sha256 по именам и sha256 содержимого всех файлов каталога (детерминированный порядок)."""
    if not path.is_dir():
        raise ArtifactError(f"artifact dir not found: {path}")
    h = hashlib.sha256()
    for f in sorted(p for p in path.iterdir() if p.is_file()):
        h.update(f.name.encode("utf-8") + b"\0")
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


def write_artifact(
    path: Path, *, model_text: str, calibrator: Calibrator, meta: dict[str, Any]
) -> str:
    """Пишет новый артефакт и возвращает его sha256. Существующий каталог не перезаписывается."""
    if path.exists():
        raise ArtifactError(f"artifact already exists (immutable): {path}")
    path.mkdir(parents=True)
    (path / "model.txt").write_bytes(model_text.replace("\r\n", "\n").encode("utf-8"))
    (path / "calibrator.json").write_bytes(_dump(calibrator.to_json_obj()))
    (path / "meta.json").write_bytes(_dump(meta))
    return artifact_sha256(path)


class Predictor:
    """Загруженная модель + калибратор. Создаётся только реестром (`ModelRegistry.load`)."""

    def __init__(
        self, booster: "lightgbm.Booster", calibrator: Calibrator, meta: dict[str, Any]
    ) -> None:
        self._booster = booster
        self._calibrator = calibrator
        self.meta = meta
        self.feature_names: list[str] = list(meta["features"])

    def predict_proba(self, fvs: list[FeatureVector]) -> npt.NDArray[np.float64]:
        """Калиброванные вероятности (n, 3) в порядке CLASS_NAMES; инференс в 1 поток."""
        for fv in fvs:
            if list(fv.values) != self.feature_names:
                raise ArtifactError("feature vector does not match model features")
        x = np.array([fv.as_list() for fv in fvs], dtype=np.float64)
        raw = np.asarray(self._booster.predict(x, num_threads=1), dtype=np.float64)
        return self._calibrator.apply(raw)

    def feature_importance(self) -> dict[str, float]:
        """Важность признаков (gain LightGBM) — для выбора топ-N в PSI-мониторинге (§5.4)."""
        gains = self._booster.feature_importance(importance_type="gain")
        return {name: float(g) for name, g in zip(self.feature_names, gains, strict=True)}


def read_artifact(path: Path) -> Predictor:
    """Читает артефакт без проверки хэша — вызывать только из `ModelRegistry.load`."""
    missing = [f for f in ARTIFACT_FILES if not (path / f).is_file()]
    if missing:
        raise ArtifactError(f"artifact incomplete, missing {missing}")
    try:
        import lightgbm
    except ImportError as e:  # extra `ml` не установлен
        raise ArtifactError("lightgbm is not installed (pip install augur[ml])") from e
    booster = lightgbm.Booster(model_str=(path / "model.txt").read_text(encoding="utf-8"))
    calibrator = Calibrator.model_validate_json((path / "calibrator.json").read_bytes())
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    return Predictor(booster, calibrator, meta)
