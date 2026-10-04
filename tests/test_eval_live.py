"""Benchmark runs against a real model: limits, a total cost cap, honest labels.

No model is called: `run_case` is replaced by one that reports a fixed cost.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from scrutai.cli import app
from scrutai.config import ScrutaiConfig
from scrutai.eval import harness
from scrutai.eval.harness import BUNDLED_CASES, Case, CaseResult, markdown_report, run_benchmark

LIVE = ScrutaiConfig(llm_mode="live")


def fake_case(cost: float) -> Any:
    def run(case: Case, config: ScrutaiConfig) -> CaseResult:
        return CaseResult(
            case=case,
            reported=list(case.labels),
            raw=list(case.labels),
            tokens=1000,
            rounds=1,
            cost=cost,
        )

    return run


def test_limit_runs_only_the_first_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "run_case", fake_case(0.01))
    seen: list[str] = []
    metrics = run_benchmark(BUNDLED_CASES, LIVE, on_case=lambda d, t, c: seen.append(c), limit=5)
    assert metrics["cases"] == 5 and metrics["cases_planned"] == 5 and len(seen) == 5
    assert metrics["total_cost_usd"] == 0.05 and metrics["avg_cost_per_case"] == 0.01
    assert metrics["stopped_early"] is False and metrics["llm_mode"] == "live"


def test_total_cost_cap_stops_after_the_case_that_reaches_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harness, "run_case", fake_case(0.4))
    details: list[CaseResult] = []
    metrics = run_benchmark(BUNDLED_CASES, LIVE, details, max_total_cost=1.0)
    assert metrics["cases"] == 3 == len(details)  # 0.4, 0.8, 1.2 -> stop
    assert metrics["stopped_early"] is True and metrics["cases_planned"] == 50
    report = markdown_report(metrics, details)
    assert "Live model run" in report and "Stopped at the cost cap after 3 of 50 cases" in report


def test_running_cost_is_visible_to_the_progress_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harness, "run_case", fake_case(0.25))
    details: list[CaseResult] = []
    spent: list[float] = []
    run_benchmark(
        BUNDLED_CASES,
        LIVE,
        details,
        lambda d, t, c: spent.append(sum(r.cost for r in details)),
        limit=3,
    )
    assert spent == [0.25, 0.5, 0.75]


def test_reports_say_which_model_produced_the_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = markdown_report(run_benchmark(BUNDLED_CASES, ScrutaiConfig(), limit=2), [])
    assert "Pipeline check (mock model)" in mock and "not a measure of any LLM" in mock
    monkeypatch.setattr(harness, "run_case", fake_case(0.02))
    live = markdown_report(run_benchmark(BUNDLED_CASES, LIVE, limit=2), [])
    assert "Live model run" in live and "anthropic/claude-opus-5-5" in live
    assert "Pipeline check" not in live


def test_cli_limit_and_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(harness, "run_case", fake_case(0.3))
    cfg = tmp_path / "live.yml"
    cfg.write_text("llm_mode: live\n", encoding="utf-8")
    res = CliRunner().invoke(
        app, ["eval", "--config", str(cfg), "--limit", "5", "--max-total-cost", "0.5", "--json"]
    )
    assert res.exit_code == 0, res.output
    assert '"cases": 2' in res.output and '"stopped_early": true' in res.output
    bad = CliRunner().invoke(app, ["eval", "--limit", "0"])
    assert bad.exit_code == 2
