"""`MLPrediction` (ML_LLM_FUSION_SPEC.md §2.5). ML только прогнозирует: нет `action`/`strength`.

`prediction_id` — ULID строки `ml_predictions.id` (на него ссылается
`decision_audit.ml_prediction_id`). `expected_return_q50`, `vol_forecast` в 0.10.0 не
заполняются (квантили и волатильность — после 0.10.0).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from augur.fusion.ml.regime import RegimeTag

__all__ = ["CLASS_NAMES", "Direction", "MLPrediction"]

Direction = Literal["long", "flat", "short"]
CLASS_NAMES: tuple[Direction, Direction, Direction] = ("short", "flat", "long")  # = labels.py


class MLPrediction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prediction_id: str
    asset: str = Field(min_length=1)
    timestamp: int = Field(ge=0)  # close_time бара, Unix ms UTC
    direction: Direction
    confidence: float = Field(ge=0.0, le=1.0)  # калиброванная P(direction)
    class_probs: dict[Direction, float]
    horizon: str = Field(pattern=r"^[0-9]+(m|h|d)$")
    expected_return_q50: float | None = None
    vol_forecast: float | None = Field(default=None, ge=0)
    regime_tag: RegimeTag
    model_version: str = Field(pattern=r"^lightgbm_v\d+$")
    feature_set_version: str
    input_hash: str
