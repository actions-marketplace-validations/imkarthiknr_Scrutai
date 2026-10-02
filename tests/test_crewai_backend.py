"""The CrewAI backend: protocol bridge, parity with native, sandboxing, failure modes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

pytest.importorskip("crewai")

from scrutai import review_diff  # noqa: E402
from scrutai.agents import CorrectnessAgent, SecurityAgent, agent_class  # noqa: E402
from scrutai.agents.crewai_backend import BridgeLLM  # noqa: E402
from scrutai.config import ScrutaiConfig  # noqa: E402
from scrutai.llm import MockLLMClient  # noqa: E402
from scrutai.models import ChangedFile, DiffContext  # noqa: E402
from scrutai.trace import Tracer  # noqa: E402

CREW = {"*": "crewai"}
PATCH = (
    "+def run(cmd, opts=[]):\n"
    "+    try:\n"
    "+        os.system(cmd)\n"
    "+    except Exception:\n"
    "+        pass\n"
)


def _diff(root: Path, patch: str = PATCH) -> DiffContext:
    (root / "lib").mkdir(exist_ok=True)
    return DiffContext(repo_root=str(root), files=[ChangedFile(path="lib/run.py", patch=patch)])


def _snapshot(
    diff: DiffContext, backends: dict[str, str]
) -> list[tuple[str, int | None, str, str]]:
    r = review_diff(diff, ScrutaiConfig(backends=backends), MockLLMClient())
    return [(f.agent, f.line, f.category, f.severity.value) for f in r.findings]


def test_bridge_parses_crewai_steps() -> None:
    messages = [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "Current Task: ..."},
        {
            "role": "assistant",
            "content": 'Thought: look\nAction: grep\nAction Input: {"pattern": "eval("}\n'
            "Observation: a.py:1:eval(x)\nb.py:2:eval(y)",
        },
        {"role": "user", "content": "Analyze the tool result."},
    ]
    assert BridgeLLM.steps_from(messages) == [
        ("grep", '{"pattern": "eval("}', "a.py:1:eval(x)\nb.py:2:eval(y)")
    ]


def test_bridge_translates_replies_both_ways() -> None:
    bridge = BridgeLLM(MockLLMClient(), "m", "sys", ["ctx"], ("findings",), None, 4)
    action = bridge.to_crewai(
        json.dumps({"thought": "check", "action": {"tool": "grep", "args": {"pattern": "x"}}})
    )
    assert action == 'Thought: check\nAction: grep\nAction Input: {"pattern": "x"}'
    final = bridge.to_crewai(json.dumps({"findings": []}))
    assert final.startswith("Thought: I now know the final answer\nFinal Answer: ")
    assert json.loads(final.split("Final Answer: ", 1)[1]) == {"findings": []}
    assert "Final Answer:" in bridge.to_crewai("not json at all")


def test_parity_with_native(tmp_path: Path) -> None:
    diff = _diff(tmp_path)
    native = _snapshot(diff, {})
    assert len(native) >= 4
    assert _snapshot(diff, CREW) == native


def test_mixed_backends(tmp_path: Path) -> None:
    diff = _diff(tmp_path)
    assert _snapshot(diff, {"security": "crewai"}) == _snapshot(diff, {})


def test_debate_runs_through_crewai(tmp_path: Path) -> None:
    result = review_diff(_diff(tmp_path), ScrutaiConfig(backends=CREW), MockLLMClient())
    debated = [f for f in result.findings if "defense: submitted" in f.history]
    assert debated and all(f.history[-1].startswith("round 2") for f in debated)


def test_crewai_tools_stay_sandboxed(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("hunter2")
    repo = tmp_path / "repo"
    repo.mkdir()

    class Snoop(MockLLMClient):
        prompts: list[str] = []

        def complete(self, *, model: str, system: str, prompt: str) -> str:
            self.prompts.append(prompt)
            if "--- STEP 1" not in prompt:
                return json.dumps(
                    {"action": {"tool": "read_file", "args": {"path": "../secret.txt"}}}
                )
            return json.dumps({"findings": []})

    llm = Snoop()
    agent = agent_class("correctness", "crewai")(llm, ScrutaiConfig())
    agent.review(_diff(repo, "+x = 1\n"))
    observed = [p for p in llm.prompts if "--- STEP 1" in p]
    assert observed and "no such file in repo" in observed[0]
    assert "hunter2" not in "".join(llm.prompts)


def test_budget_still_applies(tmp_path: Path) -> None:
    result = review_diff(
        _diff(tmp_path), ScrutaiConfig(backends=CREW, token_budget=500), MockLLMClient()
    )
    assert result.budget_exhausted and all(not f.unjudged for f in result.findings)


def test_garbage_model_means_no_findings_not_a_crash(tmp_path: Path) -> None:
    class Garbage(MockLLMClient):
        def complete(self, *, model: str, system: str, prompt: str) -> str:
            return "¯\\_(ツ)_/¯"

    agent = agent_class("security", "crewai")(Garbage(), ScrutaiConfig())
    assert agent.review(_diff(tmp_path)) == []


def test_classes_and_trace_report_the_backend(tmp_path: Path) -> None:
    cls = agent_class("security", "crewai")
    assert issubclass(cls, SecurityAgent) and cls.__name__ == "CrewAISecurityAgent"
    assert agent_class("security", "crewai") is cls  # built once
    assert agent_class("correctness") is CorrectnessAgent
    events: list[dict[str, object]] = []
    review_diff(
        _diff(tmp_path),
        ScrutaiConfig(backends={"security": "crewai"}),
        MockLLMClient(),
        Tracer(listeners=[events.append]),
    )
    backends = {
        e["agent"]: e["backend"]
        for e in events
        if e["kind"] == "node" and e.get("name") == "specialist"
    }
    assert backends["security"] == "crewai" and backends["correctness"] == "native"


@pytest.mark.parametrize("bad", [{"security": "autogen"}, {"telepathy": "crewai"}])
def test_backend_config_validation(bad: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        ScrutaiConfig(backends=bad)


def test_eval_compare(tmp_path: Path) -> None:
    from scrutai.eval.harness import compare_backends

    bench = tmp_path / "b.jsonl"
    bench.write_text(
        json.dumps(
            {"id": "a", "file": "x.py", "patch": "+os.system(cmd)\n", "labels": ["injection"]}
        )
        + "\n"
        + json.dumps({"id": "b", "file": "y.py", "patch": "+# os.system(cmd)\n", "labels": []})
        + "\n"
    )
    out = compare_backends(bench, ScrutaiConfig(), ["native", "crewai"])
    assert out["agreement_with_native"] == {"crewai": 1.0}
    assert out["backends"]["crewai"]["precision"] == out["backends"]["native"]["precision"] == 1.0
