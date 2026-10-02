"""CrewAI-backed specialists: the framework seam, exercised.

A CrewAI specialist keeps everything a native one has (role, categories,
prompts, finding parsing, the critic debate) and swaps only the agent loop
(`Specialist._loop`) for a CrewAI `Agent` + `Task` + `Crew`:

* CrewAI owns the loop: its executor drives Thought / Action / Observation,
  dispatches tools, validates tool arguments and enforces `max_iter`.
* Scrutai's tools are exposed as CrewAI `BaseTool`s that call the same
  sandboxed `Toolbox`, so path confinement, tracing and the call trace that
  becomes finding evidence are unchanged.
* The model is reached through `BridgeLLM`, a CrewAI `BaseLLM` that wraps
  Scrutai's own `LLMClient`. Budget caps, cost accounting, tracing and
  mock/live mode therefore apply exactly as for native agents, and a
  native-vs-CrewAI comparison holds the model and protocol constant: only the
  orchestration framework differs.

The bridge translates between CrewAI's text ReAct protocol and Scrutai's JSON
protocol in both directions.

Install with `pip install "scrutai[crewai]"`; enable per agent with
`backends: {security: crewai}` (or `"*": crewai` for all).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ..config import ScrutaiConfig
from ..llm import LLMClient, LLMError, extract_json
from ..tools import Toolbox
from .base import Specialist, is_answer

# CrewAI phones home by default; a code reviewer must not send anything anywhere
# it wasn't told to. Set before crewai is imported.
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")

from crewai import Agent, BaseLLM, Crew, Task  # noqa: E402
from crewai.tools import BaseTool  # noqa: E402
from pydantic import BaseModel, Field, PrivateAttr, create_model  # noqa: E402

# CrewAI appends the tool result to the assistant turn that requested it.
_STEP = re.compile(
    r"Action:\s*(?P<tool>[^\n]+?)\s*\nAction Input:\s*(?P<args>[^\n]*)\s*\n\s*"
    r"Observation:\s*(?P<obs>.*)\Z",
    re.DOTALL,
)

# Argument schemas CrewAI validates Action Input against.
_SCHEMAS: dict[str, type[BaseModel]] = {
    "read_file": create_model(
        "ReadFileArgs",
        path=(str, Field(description="repo-relative path")),
        start=(int | None, None),
        end=(int | None, None),
    ),
    "grep": create_model(
        "GrepArgs",
        pattern=(str, Field(description="text (or regex) to search for")),
        regex=(bool, False),
        glob=(str, ""),
    ),
    "git_blame": create_model(
        "GitBlameArgs", path=(str, Field(description="repo-relative path")), line=(int, ...)
    ),
    "semgrep": create_model("SemgrepArgs", paths=(list[str] | None, None)),
}


class ToolboxTool(BaseTool):
    """One Scrutai tool, callable by a CrewAI agent."""

    _toolbox: Toolbox = PrivateAttr()

    def __init__(self, name: str, toolbox: Toolbox) -> None:
        from ..tools import TOOLS

        super().__init__(
            name=name,
            description=TOOLS[name].description,
            args_schema=_SCHEMAS.get(name, create_model(f"{name}Args")),
        )
        self._toolbox = toolbox

    def _run(self, **kwargs: Any) -> str:
        args = {k: v for k, v in kwargs.items() if v is not None}
        return self._toolbox.run(self.name, args)


class BridgeLLM(BaseLLM):
    """A CrewAI LLM that speaks to Scrutai's LLMClient in Scrutai's protocol."""

    _client: LLMClient = PrivateAttr()
    _system: str = PrivateAttr()
    _transcript: list[str] = PrivateAttr()
    _answer_keys: tuple[str, ...] = PrivateAttr()
    _final_note: str | None = PrivateAttr()
    _max_steps: int = PrivateAttr()

    def __init__(
        self,
        client: LLMClient,
        model: str,
        system: str,
        transcript: list[str],
        answer_keys: tuple[str, ...],
        final_note: str | None,
        max_steps: int,
    ) -> None:
        super().__init__(model=model)
        self._client = client
        self._system = system
        self._transcript = transcript
        self._answer_keys = answer_keys
        self._final_note = final_note
        self._max_steps = max_steps

    def supports_function_calling(self) -> bool:
        return False  # use CrewAI's text ReAct, which the bridge translates

    def call(
        self,
        messages: Any,
        tools: Any = None,
        callbacks: Any = None,
        available_functions: Any = None,
        from_task: Any = None,
        from_agent: Any = None,
        response_model: Any = None,
    ) -> str:
        steps = self.steps_from(messages)
        prompt = list(self._transcript)
        for i, (tool, args, obs) in enumerate(steps, 1):
            prompt.append(f"--- STEP {i}\nACTION: {tool} {args}\nOBSERVATION:\n{obs}")
        if self._final_note and len(steps) >= self._max_steps - 1:
            prompt.append(self._final_note)
        raw = self._client.complete(model=self.model, system=self._system, prompt="\n".join(prompt))
        return self.to_crewai(raw)

    @staticmethod
    def steps_from(messages: Any) -> list[tuple[str, str, str]]:
        if not isinstance(messages, list):
            return []
        out: list[tuple[str, str, str]] = []
        for m in messages:
            if m.get("role") != "assistant":
                continue
            match = _STEP.search(str(m.get("content", "")))
            if match:
                out.append((match["tool"], match["args"].strip(), match["obs"].strip()))
        return out

    def to_crewai(self, raw: str) -> str:
        try:
            payload = extract_json(raw)
        except json.JSONDecodeError:
            return 'Thought: I could not produce a valid answer.\nFinal Answer: {"findings": []}'
        thought = str(payload.get("thought") or "").replace("\n", " ").strip() or "Continuing."
        action = payload.get("action")
        if not is_answer(payload, self._answer_keys) and isinstance(action, dict):
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            return (
                f"Thought: {thought}\nAction: {action.get('tool', '')}\n"
                f"Action Input: {json.dumps(args)}"
            )
        return f"Thought: I now know the final answer\nFinal Answer: {json.dumps(payload)}"


class CrewAIMixin:
    """Replaces Specialist._loop with a CrewAI crew; everything else is inherited."""

    backend = "crewai"
    # Provided by Specialist:
    name: str
    role: str
    llm: LLMClient
    config: ScrutaiConfig

    def _loop(
        self,
        system: str,
        transcript: list[str],
        toolbox: Toolbox,
        answer_keys: tuple[str, ...],
        final_note: str | None = None,
    ) -> dict[str, Any] | None:
        steps = max(self.config.max_agent_steps, 1)
        bridge = BridgeLLM(
            self.llm,
            self.config.models.specialist,
            system,
            transcript,
            answer_keys,
            final_note,
            steps,
        )
        agent = Agent(
            role=f"{self.name} reviewer",
            goal=self.role or f"Review code changes for {self.name} issues.",
            backstory="A code review panelist whose findings face an adversarial critic.",
            tools=[ToolboxTool(n, toolbox) for n in toolbox.allowed],
            llm=bridge,
            max_iter=steps,
            allow_delegation=False,
            verbose=False,
        )
        task = Task(
            description="\n".join(transcript),
            expected_output=f"A JSON object with one of these keys: {', '.join(answer_keys)}.",
            agent=agent,
        )
        try:
            output = Crew(agents=[agent], tasks=[task], verbose=False).kickoff()
            payload = extract_json(str(getattr(output, "raw", "")))
        except (LLMError, json.JSONDecodeError):
            return None
        except Exception:  # noqa: BLE001 - a framework failure must not kill the review
            return None
        return payload if is_answer(payload, answer_keys) else None


_VARIANTS: dict[str, type[Specialist]] = {}


def crewai_specialist(base: type[Specialist]) -> type[Specialist]:
    """The CrewAI-backed variant of a native specialist class (built once)."""
    if base.name not in _VARIANTS:
        _VARIANTS[base.name] = type(
            f"CrewAI{base.__name__}", (CrewAIMixin, base), {"__test__": False}
        )
    return _VARIANTS[base.name]
