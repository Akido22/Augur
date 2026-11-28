"""LocalFusionClient e2e: ohlcv → ML (production) + LLM-контекст → fuse → Signal Engine →
decision_audit с версиями lightgbm_v1 / prompt_context_v1 → replay (FUSION SPEC §5.5, §6.2)."""

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("lightgbm")

from typer.testing import CliRunner  # noqa: E402

from augur.config.settings import FusionSettings, LLMProvider, LLMSettings, RiskLimits  # noqa: E402
from augur.fusion.llm.inputs import PriceSummary  # noqa: E402
from augur.fusion.llm.service import LLMContextService  # noqa: E402
from augur.fusion.local import LocalFusionClient, read_breaker_open, read_position_side  # noqa: E402
from augur.fusion.ml.inference import MLService  # noqa: E402
from augur.fusion.ml.registry import ModelRegistry  # noqa: E402
from augur.fusion.replay import ReplayError, replay_fused  # noqa: E402
from augur.ids import new_ulid  # noqa: E402
from augur.risk import DecisionAuditWriter, RiskGate  # noqa: E402
from augur.signals import SignalEngine, SignalRepository  # noqa: E402
from augur.signals.engine import StrategySignal  # noqa: E402
from augur.signals.ready import ms_to_dt  # noqa: E402
from augur.strategies.base import SignalAction  # noqa: E402
from tests._risk import make_ctx  # noqa: E402
from tests.fusion._ml import (  # noqa: E402
    Clock,
    insert_ohlcv,
    promote_to_production,
    small_cfg,
    synth_bars,
    train_register,
)
from tests.fusion.llm._fake import FakeLLM, ctx_json  # noqa: E402

VENUE = "ccxt:binance"
SYM = "BTC/USDT"


def _setup(db: sqlite3.Connection, tmp_path: Path, llm_reply: str | None):
    bars = synth_bars(1400)
    insert_ohlcv(db, bars, SYM, VENUE)
    cfg = small_cfg()
    clock = Clock(int(bars.close_time(len(bars) - 1)))
    v = train_register(db, bars, cfg, tmp_path, clock)
    promote_to_production(db, v, cfg, clock)
    # торговый бар — последний закрытый; «сейчас» — сразу после его закрытия
    last = len(bars) - 1
    bar_close = bars.close_time(last)
    clock.now = bar_close + 1_000
    lcfg = LLMSettings(
        mode="enabled" if llm_reply else "disabled",
        providers=[LLMProvider(name="p", base_url="https://x/v1", model="m")],
    )
    fs = FusionSettings(enabled=True, ml=cfg, llm=lcfg)
    llm = None
    if llm_reply is not None:
        llm = LLMContextService(db, lcfg, FakeLLM([llm_reply]), {}, clock=clock)
    ml = MLService(db, ModelRegistry(db, clock=clock), cfg, clock=clock)
    client = LocalFusionClient(db, fs, ml=ml, llm=llm, clock=clock)
    return bars, last, clock, client, llm, fs


def _strategy_signal(bars, i: int, action: SignalAction) -> StrategySignal:  # type: ignore[no-untyped-def]
    return StrategySignal(
        signal_id=new_ulid(),
        strategy_name="sma_cross",
        strategy_version="sma_cross@1.0.0",
        params_hash="p" * 64,
        data_hash="d" * 64,
        symbol=SYM,
        timeframe="1h",
        action=action,
        strength=0.8,
        reason="t",
        timestamp=int(bars.ts_open[i]),
    )


async def test_e2e_fused_signal_versions_and_replay(db: sqlite3.Connection, tmp_path: Path) -> None:
    bars, last, clock, client, _, fs = _setup(db, tmp_path, None)
    as_of = bars.close_time(last)
    iso = datetime.fromtimestamp(as_of / 1000, tz=UTC).isoformat().replace("+00:00", "Z")
    lcfg = fs.llm.model_copy(update={"mode": "enabled"})
    fs = fs.model_copy(update={"llm": lcfg})
    llm = LLMContextService(
        db, lcfg, FakeLLM([ctx_json(0.0, 0.2, as_of=iso)]), {}, clock=lambda: as_of
    )
    res = await llm.refresh(
        SYM, as_of=as_of, news=[], price=PriceSummary(window_h=6, ret=0, vol_z=0)
    )
    assert res.status == "ok"
    client = LocalFusionClient(db, fs, ml=client._ml, llm=llm, clock=clock)  # noqa: SLF001

    repo = SignalRepository(db, config_hash="c" * 64, code_version="0.10.0")
    eng = SignalEngine(client, clock=clock, fusion_enabled=True, fusion_timeout_ms=5_000, repo=repo)
    sig = await eng.resolve_signal([_strategy_signal(bars, last, SignalAction.BUY)])
    assert sig is not None and sig.source == "fusion", db.execute(
        "SELECT event, payload_json FROM audit_log"
    ).fetchall()
    row = db.execute(
        "SELECT fusion_config_version, source_breakdown_json FROM fused_signals WHERE id=?",
        (sig.fused_signal_id,),
    ).fetchone()
    bd = json.loads(row[1])
    assert row[0] == fs.config.config_version
    assert bd["ml"]["model_version"] == "lightgbm_v1" and bd["llm"]["status"] == "ok"
    assert bd["llm"]["prompt_version"] == "prompt_context_v1"
    assert db.execute("SELECT COUNT(*) FROM ml_predictions").fetchone() == (1,)

    writer = DecisionAuditWriter(db, app_version="0.10.0", config_hash="c" * 64)
    verdict = RiskGate(RiskLimits()).evaluate_sync(sig, make_ctx(now=ms_to_dt(clock.now)))
    rec = writer.record(sig, verdict)
    assert (rec.ml_model_version, rec.llm_prompt_version, rec.llm_status) == (
        "lightgbm_v1",
        "prompt_context_v1",
        "ok",
    )
    assert rec.ml_prediction_id == bd["ml"]["prediction_id"]

    rep = replay_fused(db, rec.id, fs.config)  # по id decision_audit
    assert rep.match, rep.diffs
    with pytest.raises(ReplayError, match="fusion_config_version"):
        replay_fused(db, rec.id, fs.config.model_copy(update={"config_version": "other"}))
    # запись, расходящаяся с пересчётом, обнаруживается
    probe = sqlite3.connect(":memory:")
    db.backup(probe)
    probe.execute("UPDATE fused_signals SET reason='forged' WHERE id=?", (sig.fused_signal_id,))
    forged = replay_fused(probe, sig.fused_signal_id, fs.config)  # type: ignore[arg-type]
    assert not forged.match and "reason" in forged.diffs

    # CLI: augur fusion replay
    from augur.interfaces.cli import main as cli

    db_file = tmp_path / "x.db"
    disk = sqlite3.connect(db_file)
    db.backup(disk)
    disk.close()
    disk = sqlite3.connect(db_file)
    disk.execute(
        "INSERT INTO schema_migrations(version, name, checksum, compat, app_version) "
        "VALUES (8, '0008_notify', 'x', 'expand', '0.11.0')"
    )
    disk.commit()
    disk.close()
    runner = CliRunner()
    os.environ["API_AUTH_TOKEN"] = "t"  # noqa: S105 — тестовый токен
    try:
        out = runner.invoke(cli.app, ["fusion", "replay", rec.id, "--db", str(db_file)])
    finally:
        del os.environ["API_AUTH_TOKEN"]
    assert out.exit_code == 0 and "MATCH" in out.output, out.output


async def test_ml_unavailable_gives_no_signal_not_crash(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    bars, last, clock, client, _, fs = _setup(db, tmp_path, None)
    db.execute("UPDATE ml_models SET status='retired'")  # нет production-модели
    db.commit()
    repo = SignalRepository(db, config_hash="c" * 64, code_version="0.10.0")
    eng = SignalEngine(client, clock=clock, fusion_enabled=True, fusion_timeout_ms=5_000, repo=repo)
    sig = await eng.resolve_signal([_strategy_signal(bars, last, SignalAction.BUY)])
    assert sig is not None and sig.source == "fusion" and sig.action == SignalAction.NO_SIGNAL
    writer = DecisionAuditWriter(db, app_version="0.10.0", config_hash="c" * 64)
    fv = writer.fusion_versions(sig.fused_signal_id)
    assert fv["ml_model_version"] == "n/a" and fv["llm_status"] is None
    assert db.execute("SELECT 1 FROM audit_log WHERE event='ML_MODEL_UNAVAILABLE'").fetchone()


def test_state_readers(db: sqlite3.Connection) -> None:
    assert read_position_side(db, SYM) == 0 and read_breaker_open(db) is False
    db.execute("INSERT INTO cb_state(id, state) VALUES (1, 'TRIPPED')")
    db.execute(
        "INSERT INTO positions(venue, symbol, mode, qty, avg_price, updated_ts) "
        "VALUES ('paper', ?, 'PAPER', '-0.5', '100', 0)",
        (SYM,),
    )
    assert read_position_side(db, SYM) == -1 and read_breaker_open(db) is True
