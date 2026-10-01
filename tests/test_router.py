from __future__ import annotations

import json

from scrutai import review_diff
from scrutai.config import ScrutaiConfig
from scrutai.llm import MockLLMClient, extract_json
from scrutai.models import ChangedFile, DiffContext, Verdict
from scrutai.router import heuristic_route, llm_route, route


def _diff(*files: tuple[str, str]) -> DiffContext:
    return DiffContext(files=[ChangedFile(path=p, patch=patch) for p, patch in files])


ALL = ScrutaiConfig(enabled_agents=["security", "correctness", "tests", "performance", "style"])


def test_docs_only_wakes_nobody() -> None:
    diff = _diff(("README.md", "+Some new docs\n"), ("uv.lock", "+hash = 1\n"))
    assert heuristic_route(diff, ALL) == []
    result = review_diff(diff, ScrutaiConfig(), MockLLMClient())
    assert result.agents == []
    assert result.verdict == Verdict.APPROVE
    assert "Nothing to review" in result.summary


def test_plain_code_skips_security() -> None:
    diff = _diff(("calc.py", "+def add(a, b):\n+    return a + b\n"))
    assert heuristic_route(diff, ALL) == ["correctness", "tests", "style"]


def test_risky_code_wakes_security() -> None:
    diff = _diff(("run.py", "+import subprocess\n+subprocess.run(cmd, shell=True)\n"))
    assert "security" in heuristic_route(diff, ALL)


def test_sensitive_path_wakes_security() -> None:
    diff = _diff(("app/auth/views.py", "+x = 1\n"))
    assert "security" in heuristic_route(diff, ALL)


def test_test_only_change_skips_tests_agent() -> None:
    diff = _diff(("tests/test_calc.py", "+def test_add():\n+    assert add(1, 2) == 3\n"))
    selected = heuristic_route(diff, ALL)
    assert "tests" not in selected and "correctness" not in selected


def test_respects_enabled_agents_order() -> None:
    cfg = ScrutaiConfig(enabled_agents=["tests", "correctness"])
    diff = _diff(("calc.py", "+def add(a, b):\n+    return a + b\n"))
    assert heuristic_route(diff, cfg) == ["tests", "correctness"]


class _Router:
    tokens_used = 0
    cost_usd = 0.0

    def __init__(self, reply: str) -> None:
        self.reply = reply

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        return self.reply


def test_llm_router_can_only_narrow() -> None:
    diff = _diff(("calc.py", "+def add(a, b):\n+    return a + b\n"))
    reply = json.dumps({"agents": ["tests", "security"]})
    assert llm_route(diff, ALL, _Router(reply), ["correctness", "tests"]) == ["tests"]


def test_llm_router_garbage_falls_back() -> None:
    diff = _diff(("calc.py", "+def add(a, b):\n+    return a + b\n"))
    assert llm_route(diff, ALL, _Router("not json"), ["correctness", "tests"]) == [
        "correctness",
        "tests",
    ]


def test_route_uses_llm_only_when_configured() -> None:
    diff = _diff(("calc.py", "+def add(a, b):\n+    return a + b\n"))
    cfg = ScrutaiConfig(enabled_agents=["correctness", "tests"], routing="llm")
    assert route(diff, cfg, _Router('{"agents": ["tests"]}')) == ["tests"]


def test_extract_json_handles_fences_and_prose() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here you go: {"a": 2} hope that helps') == {"a": 2}
