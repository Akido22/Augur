"""CLI: augur ml train / list / promote (--confirm), llm usage."""

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("lightgbm")

from typer.testing import CliRunner  # noqa: E402

from augur.interfaces.cli.main import app  # noqa: E402
from tests.fusion._ml import insert_ohlcv, synth_bars  # noqa: E402

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_AUTH_TOKEN", "t")
    monkeypatch.setenv("FUSION__ML__FEATURES__WINDOW", "120")
    monkeypatch.setenv(
        "FUSION__ML__TRAIN",
        '{"train_bars": 600, "test_bars": 200, "n_estimators": 20, "calib_min": 100}',
    )
    db = str(tmp_path / "a.db")
    assert runner.invoke(app, ["db", "migrate", "--db", db]).exit_code == 0
    conn = sqlite3.connect(db)
    insert_ohlcv(conn, synth_bars(1400), "BTC/USDT", "ccxt:binance")
    conn.close()
    return db


def test_train_list_promote(env: str) -> None:
    res = runner.invoke(app, ["ml", "train", "--symbol", "BTC/USDT", "--db", env])
    assert res.exit_code == 0, res.output
    assert "lightgbm_v1 signal_clf_btcusdt_1h status=candidate" in res.output
    assert Path("artifacts/ml/lightgbm_v1/model.txt").is_file()
    res = runner.invoke(app, ["ml", "list", "--db", env])
    assert "lightgbm_v1" in res.output and "candidate" in res.output
    res = runner.invoke(
        app, ["ml", "promote", "lightgbm_v1", "--to", "staging", "--approved-by", "me", "--db", env]
    )
    assert res.exit_code == 1 and "--confirm" in res.output
    res = runner.invoke(
        app,
        [
            "ml",
            "promote",
            "lightgbm_v1",
            "--to",
            "staging",
            "--approved-by",
            "me",
            "--confirm",
            "--db",
            env,
        ],
    )
    assert res.exit_code == 0 and "staging" in res.output
    res = runner.invoke(
        app,
        [
            "ml",
            "promote",
            "lightgbm_v1",
            "--to",
            "production",
            "--approved-by",
            "me",
            "--confirm",
            "--db",
            env,
        ],
    )
    assert res.exit_code == 1 and "staging for" in res.output  # 14 дней shadow
    res = runner.invoke(app, ["llm", "usage", "--db", env])
    assert res.exit_code == 0 and "no LLM calls today" in res.output


def test_train_without_bars_fails(env: str) -> None:
    res = runner.invoke(app, ["ml", "train", "--symbol", "ETH/USDT", "--db", env])
    assert res.exit_code == 1 and "no bars" in res.output
