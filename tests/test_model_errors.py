"""Model failures are counted and reported, never mistaken for a clean review."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from scrutai import cli
from scrutai.config import ScrutaiConfig
from scrutai.demo import demo_diff
from scrutai.eval import harness
from scrutai.eval.harness import BUNDLED_CASES, markdown_report, run_benchmark
from scrutai.llm import LLMError, MockLLMClient
from scrutai.models import Verdict
from scrutai.orchestrator import review_diff


class Failing:
    """A provider that rejects every call, like a missing or invalid API key."""

    tokens_used = 0
    cost_usd = 0.0

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        raise LLMError(f"{model}: AuthenticationError: invalid x-api-key")


class Flaky(MockLLMClient):
    """The mock model, but every third call fails."""

    def __init__(self) -> None:
        super().__init__()
        self.n = 0

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        self.n += 1
        if self.n % 3 == 0:
            raise LLMError(f"{model}: RateLimitError: slow down")
        return super().complete(model=model, system=system, prompt=prompt)


def test_every_call_failing_is_not_a_clean_review() -> None:
    with demo_diff() as diff:
        result = review_diff(diff, ScrutaiConfig(), Failing())
    assert result.model_unavailable and result.model_calls == result.model_errors > 0
    assert "invalid x-api-key" in (result.model_error or "")
    assert result.summary.startswith("No model call succeeded")
    assert result.verdict != Verdict.APPROVE and result.findings == []


def test_some_calls_failing_is_reported() -> None:
    with demo_diff() as diff:
        result = review_diff(diff, ScrutaiConfig(), Flaky())
    assert 0 < result.model_errors < result.model_calls and not result.model_unavailable
    assert "model call(s) failed" in result.summary and "RateLimitError" in (
        result.model_error or ""
    )


def test_a_healthy_review_reports_no_errors() -> None:
    with demo_diff() as diff:
        result = review_diff(diff, ScrutaiConfig(), MockLLMClient())
    assert result.model_errors == 0 and result.model_error is None and result.model_calls > 0


def test_cli_review_exits_2_when_no_model_call_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "make_client", lambda mode: Failing())
    res = CliRunner().invoke(cli.app, ["review", "--demo"])
    assert res.exit_code == 2 and "No model call succeeded" in res.output
    assert "invalid x-api-key" in res.output


def test_eval_stops_after_the_first_case_when_every_call_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(harness, "make_client", lambda mode: Failing())
    cfg = tmp_path / "live.yml"
    cfg.write_text("llm_mode: live\n", encoding="utf-8")
    res = CliRunner().invoke(cli.app, ["eval", "--config", str(cfg), "--limit", "5"])
    assert res.exit_code == 2 and "Every model call failed" in res.output
    assert "[2/5]" not in res.output  # stopped after case 1


def test_report_flags_failed_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "make_client", lambda mode: Failing())
    details: list[Any] = []
    metrics = run_benchmark(BUNDLED_CASES, ScrutaiConfig(llm_mode="live"), details, limit=2)
    report = markdown_report(metrics, details)
    assert metrics["model_errors"] == metrics["model_calls"] > 0
    assert "Every model call failed: these numbers are meaningless" in report
