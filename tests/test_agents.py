"""The ReAct loop, the toolbox, and the agents' parsing, against scripted models."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from scrutai.agents import CorrectnessAgent, SecurityAgent, TestCoverageAgent
from scrutai.config import ScrutaiConfig
from scrutai.llm import MockLLMClient
from scrutai.models import ChangedFile, DiffContext, Severity
from scrutai.tools import Toolbox, read_file


class Scripted:
    """Replays canned replies and records every prompt it was sent."""

    tokens_used = 0
    cost_usd = 0.0

    def __init__(self, *replies: object) -> None:
        self.replies = [r if isinstance(r, str) else json.dumps(r) for r in replies]
        self.prompts: list[str] = []

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else '{"findings": []}'


def _diff(patch: str, path: str = "app.py", root: str = ".") -> DiffContext:
    return DiffContext(repo_root=root, files=[ChangedFile(path=path, patch=patch)])


def test_react_loop_acts_observes_then_reports(tmp_path: Path) -> None:
    (tmp_path / "helpers.py").write_text("def sanitize(x):\n    return x\n")
    llm = Scripted(
        {"action": {"tool": "grep", "args": {"pattern": "sanitize"}}},
        {
            "findings": [
                {
                    "title": "Unsanitized shell call",
                    "file": "app.py",
                    "line": 1,
                    "category": "Injection",
                    "severity": "HIGH",
                    "confidence": 3,
                    "evidence": ["L1: os.system(x)"],
                }
            ]
        },
    )
    agent = SecurityAgent(llm, ScrutaiConfig())
    findings = agent.review(_diff("+os.system(x)\n", root=str(tmp_path)))

    assert "OBSERVATION:\nhelpers.py:1:def sanitize(x):" in llm.prompts[1]
    (f,) = findings
    assert (f.category, f.severity, f.confidence, f.line) == ("injection", Severity.HIGH, 1.0, 1)
    assert any(e.startswith("tool:grep(") for e in f.evidence)


def test_step_budget_forces_a_final_answer() -> None:
    action = {"action": {"tool": "grep", "args": {"pattern": "x"}}}
    llm = Scripted(action, action, action, action, action)
    agent = CorrectnessAgent(llm, ScrutaiConfig(max_agent_steps=3))
    assert agent.review(_diff("+x = 1\n")) == []
    assert len(llm.prompts) == 3
    assert "--- FINAL" in llm.prompts[-1]


def test_garbage_reply_yields_no_findings() -> None:
    agent = CorrectnessAgent(Scripted("I think it's fine!"), ScrutaiConfig())
    assert agent.review(_diff("+x = 1\n")) == []


def test_malformed_findings_are_skipped_not_fatal() -> None:
    llm = Scripted(
        {
            "findings": [
                {"title": "ok", "line": 1, "file": "elsewhere.py"},
                {"title": "bad severity", "severity": "apocalyptic"},
                {"body": "no title"},
                "not a dict",
            ]
        }
    )
    (f,) = CorrectnessAgent(llm, ScrutaiConfig()).review(_diff("+x = 1\n"))
    assert f.file == "app.py"  # unknown path falls back to a file in the diff


def test_unknown_tool_is_an_observation_not_a_crash() -> None:
    llm = Scripted({"action": {"tool": "rm_rf", "args": {}}}, {"findings": []})
    CorrectnessAgent(llm, ScrutaiConfig()).review(_diff("+x = 1\n"))
    assert "unknown tool 'rm_rf'" in llm.prompts[1]


def test_agent_skips_files_outside_its_kinds() -> None:
    llm = Scripted()
    diff = _diff("+def test_x():\n+    pass\n", path="tests/test_x.py")
    assert CorrectnessAgent(llm, ScrutaiConfig()).review(diff) == []
    assert llm.prompts == []  # no model call at all


def test_read_file_is_confined_to_repo(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("hunter2")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("one\ntwo\nthree\n")
    assert read_file("../secret.txt", str(repo)) == ""
    assert read_file("a.py", str(repo), start=2, end=3) == "2: two\n3: three"
    box = Toolbox(str(repo))
    assert "no such file" in box.run("read_file", {"path": "../secret.txt"})
    assert "bad arguments" in box.run("read_file", {})


def test_grep_treats_pattern_as_data(git_repo: Callable[[dict[str, str]], Path]) -> None:
    repo = git_repo({"m.py": "value = eval(x)\n"})
    box = Toolbox(str(repo))
    assert "m.py:1:value = eval(x)" in box.run("grep", {"pattern": "eval("})
    assert box.run("grep", {"pattern": "--version"}) == "(no matches)"


def test_mock_security_agent_greps_the_sink_before_reporting() -> None:
    diff = _diff('+TOKEN = "abc123def"\n+os.system(cmd)\n')
    findings = SecurityAgent(MockLLMClient(), ScrutaiConfig()).review(diff)
    assert {f.category for f in findings} == {"hardcoded_secret", "injection"}
    assert all("tool:grep(pattern='os.system(')" in f.evidence[-1] for f in findings)


def test_mock_tests_agent_credits_tests_in_same_diff() -> None:
    diff = DiffContext(
        files=[
            ChangedFile(path="calc.py", patch="+def add(a, b):\n+    return a + b\n"),
            ChangedFile(path="tests/test_calc.py", patch="+def test_add():\n+    add(1, 2)\n"),
        ]
    )
    assert TestCoverageAgent(MockLLMClient(), ScrutaiConfig()).review(diff) == []
