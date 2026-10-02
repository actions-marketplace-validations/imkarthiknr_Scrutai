from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from scrutai.cli import app
from scrutai.config import ScrutaiConfig
from scrutai.eval.harness import Case, CaseResult, load_cases, run_benchmark, run_case

BENCH = "benchmark/cases.jsonl"


def _write(tmp_path: Path, *cases: dict[str, object]) -> str:
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(json.dumps(c) for c in cases) + "\n")
    return str(path)


def test_scoring_is_a_multiset() -> None:
    r = CaseResult(
        Case("c", "f.py", "", ["injection"]), ["injection", "injection", "weak_crypto"], [], 0, 1
    )
    assert r.score(r.reported) == (1, 2, 0)
    assert r.spurious == ["injection", "weak_crypto"] and r.missed == []


def test_cases_are_hermetic(tmp_path: Path) -> None:
    """A test that exists only in the case's own repo must be found; the cwd is irrelevant."""
    patch = "+def discount(p, pct):\n+    return p * (1 - pct)\n"
    covered = Case(
        "a",
        "calc.py",
        patch,
        [],
        {"tests/test_calc.py": "def test_discount():\n    discount(1, 0)\n"},
    )
    bare = Case("b", "calc.py", patch, ["missing_tests"])
    assert run_case(covered, ScrutaiConfig()).reported == []
    assert run_case(bare, ScrutaiConfig()).reported == ["missing_tests"]


def test_bundled_benchmark_quality_bar() -> None:
    """Regression gate for the mock pipeline; the README quotes these numbers."""
    m = run_benchmark(BENCH, ScrutaiConfig())
    assert m["precision"] == 1.0
    assert m["recall"] >= 0.85
    assert m["clean_case_fpr"] == 0.0
    assert m["critic_precision_lift"] > 0  # the critic must earn its tokens
    assert m["recall_without_critic"] == m["recall"]  # ...without killing true positives


def test_invalid_case_reports_line(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": "ok", "patch": "+x\\n"}\n{"id": "no-patch"}\n')
    with pytest.raises(ValueError, match="bad.jsonl:2"):
        load_cases(path)


def test_cli_gate_and_report(tmp_path: Path) -> None:
    bench = _write(
        tmp_path,
        {
            "id": "x",
            "file": "c.py",
            "patch": "+try:\n+    f()\n+except:\n+    pass\n",
            "labels": [],
        },
    )
    report = tmp_path / "r.md"
    runner = CliRunner()
    res = runner.invoke(
        app, ["eval", "--benchmark", bench, "--min-precision", "0.9", "--report", str(report)]
    )
    assert res.exit_code == 1, res.output
    assert "gate failed" in res.output
    assert "| x | - | broad_except |" in report.read_text()

    res = runner.invoke(app, ["eval", "--benchmark", BENCH, "--json", "--min-precision", "0.95"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["cases"] >= 30
