"""Golden-тесты Risk Gate: tests/golden/risk_gate/cases.json (ожидания посчитаны вручную)."""

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from augur.config.settings import RiskLimits
from augur.risk.gate import CHECK_NAMES, RiskGate
from tests._risk import make_ctx, make_signal

CASES_FILE = Path(__file__).parent / "golden" / "risk_gate" / "cases.json"
CASES: list[dict[str, Any]] = json.loads(CASES_FILE.read_text("utf-8"))["cases"]


def _dec(v: str | None) -> Decimal | None:
    return None if v is None else Decimal(v)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_golden(case: dict[str, Any]) -> None:
    sig_over = dict(case["signal"])
    if "expires_in_s" in sig_over:
        sig_over["expires_in"] = timedelta(seconds=sig_over.pop("expires_in_s"))
    if "risk_flags" in sig_over:
        sig_over["flags"] = sig_over.pop("risk_flags")
    if "stop_loss" in sig_over:
        sig_over["stop_loss"] = Decimal(sig_over["stop_loss"])
    sig = make_signal(**sig_over)
    ctx = make_ctx(**case["ctx"])
    v = RiskGate(RiskLimits(**case.get("limits", {})), id_factory=lambda: "GOLDEN").evaluate_sync(
        sig, ctx
    )
    exp = case["expected"]
    assert [c.name for c in v.checks] == list(CHECK_NAMES)
    assert v.approved is exp["approved"]
    assert v.risk_verdict == exp["risk_verdict"]
    assert [c.name for c in v.failed] == [n for n in CHECK_NAMES if n in exp["failed"]]
    assert v.sized_qty == _dec(exp["sized_qty"])
    assert v.sized_notional == _dec(exp["sized_notional"])
    if exp["sized_qty"] is not None:  # точная запись в Decimal (без лишней точности)
        assert str(v.sized_qty) == exp["sized_qty"]
