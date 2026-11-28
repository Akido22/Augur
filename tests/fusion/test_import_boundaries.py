"""Границы импорта (ARCH §5.6, CLAUDE.md): ML/LLM — только внутри augur.fusion; fusion не
импортирует execution / risk / adapters; signals, risk, execution, strategies, news не
импортируют ML-библиотеки и LLM-подпакеты."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "augur"
ML_LIBS = {"lightgbm", "numpy", "pandas", "sklearn", "torch", "anthropic", "openai"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def _files(sub: str) -> list[Path]:
    return sorted((SRC / sub).rglob("*.py"))


def test_ml_llm_libraries_only_in_fusion() -> None:
    bad = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("fusion/"):
            continue
        roots = {m.split(".")[0] for m in _imports(path)}
        if roots & ML_LIBS:
            bad.append((rel, roots & ML_LIBS))
    assert bad == []


def test_numpy_lightgbm_only_in_fusion_ml() -> None:
    bad = [
        p.relative_to(SRC).as_posix()
        for p in _files("fusion")
        if not p.relative_to(SRC).as_posix().startswith("fusion/ml/")
        and {m.split(".")[0] for m in _imports(p)} & {"lightgbm", "numpy"}
    ]
    assert bad == []


def test_fusion_never_reaches_execution() -> None:
    forbidden = ("augur.execution", "augur.risk", "augur.adapters", "augur.signals")
    bad = [
        (p.relative_to(SRC).as_posix(), m)
        for p in _files("fusion")
        for m in _imports(p)
        if m.startswith(forbidden)
    ]
    assert bad == []


def test_core_modules_do_not_import_fusion_internals() -> None:
    internals = ("augur.fusion.ml", "augur.fusion.llm", "augur.fusion.engine", "augur.fusion.local")
    bad = [
        (p.relative_to(SRC).as_posix(), m)
        for sub in ("signals", "risk", "execution", "strategies", "news", "adapters", "data")
        for p in _files(sub)
        for m in _imports(p)
        if m.startswith(internals)
    ]
    assert bad == []
