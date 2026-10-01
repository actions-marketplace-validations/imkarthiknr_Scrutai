"""The Specialist interface — the seam that keeps the system framework-agnostic.

Every specialist is a ReAct-style agent: it gathers context with real tools,
reasons via the LLM, and proposes typed Findings. The orchestrator and critic
depend only on this interface, so a CrewAI- or ADK-backed specialist can be
dropped in later without touching the graph.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod

from ..config import ScrutaiConfig
from ..llm import LLMClient
from ..models import DiffContext, Finding, Severity


class Specialist(ABC):
    name: str = "specialist"
    #: One-line role description injected into the system prompt.
    role: str = ""

    def __init__(self, llm: LLMClient, config: ScrutaiConfig) -> None:
        self.llm = llm
        self.config = config

    @abstractmethod
    def gather_context(self, diff: DiffContext) -> str:
        """ReAct 'act' step: use tools to pull the evidence this agent needs."""

    def system_prompt(self) -> str:
        return (
            f"You are the {self.name} specialist in a code review panel. {self.role} "
            'Return ONLY JSON: {"findings": [{title, body, severity, confidence, '
            "evidence[]}]}. severity in [info,low,medium,high,critical]; confidence in [0,1]."
        )

    def review(self, diff: DiffContext) -> list[Finding]:
        context = self.gather_context(diff)
        raw = self.llm.complete(
            model=self.config.models.specialist,
            system=self.system_prompt(),
            prompt=context,
        )
        return self._parse(raw, diff)

    def _parse(self, raw: str, diff: DiffContext) -> list[Finding]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return []
        findings: list[Finding] = []
        default_file = diff.paths[0] if diff.paths else "unknown"
        for item in payload.get("findings", []):
            try:
                findings.append(
                    Finding(
                        agent=self.name,
                        title=item["title"],
                        body=item.get("body", ""),
                        file=item.get("file", default_file),
                        line=item.get("line"),
                        severity=Severity(item.get("severity", "medium")),
                        confidence=float(item.get("confidence", 0.5)),
                        evidence=item.get("evidence", []),
                    )
                )
            except (KeyError, ValueError):
                continue
        return findings
