"""Deterministic smoke tests — run offline against the mock LLM.

Proves the graph wires up and the critic filters, with zero API calls. This is
the pattern that makes a non-deterministic system testable in CI.
"""

from scrutai import review_diff
from scrutai.config import ScrutaiConfig
from scrutai.llm import MockLLMClient
from scrutai.models import ChangedFile, DiffContext, Verdict


def _diff(patch: str) -> DiffContext:
    return DiffContext(files=[ChangedFile(path="x.py", patch=patch)])


def test_injection_is_flagged() -> None:
    result = review_diff(_diff("+os.system(cmd)\n"), ScrutaiConfig(), MockLLMClient())
    assert any("injection" in f.title.lower() for f in result.findings)
    assert result.verdict in (Verdict.REQUEST_CHANGES, Verdict.COMMENT)


def test_clean_diff_survives_clean() -> None:
    result = review_diff(_diff("+# just a comment\n"), ScrutaiConfig(), MockLLMClient())
    assert result.findings == []
    assert result.verdict == Verdict.APPROVE


def test_eval_harness_reports_precision() -> None:
    from scrutai.eval.harness import run_benchmark

    metrics = run_benchmark("benchmark/cases.jsonl", ScrutaiConfig())
    assert 0.0 <= metrics["precision"] <= 1.0
    assert metrics["cases"] == 4
