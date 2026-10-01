"""The Specialist interface — the seam that keeps the system framework-agnostic.

Every specialist is a ReAct agent: it reads the diff, *acts* through real repo
tools (read_file / grep / git_blame / ...), *observes* the results, and repeats
until it can propose typed Findings or its step budget runs out. The
orchestrator and critic depend only on `review()` (and `defend()` during the
critic debate), so a CrewAI- or ADK-backed specialist can override those and
drop in without touching the graph.

Protocol (one JSON object per model turn):
    {"thought": "...", "action": {"tool": "grep", "args": {"pattern": "eval("}}}
    {"thought": "...", "findings": [{title, body, file, line, category, ...}]}
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from ..config import ScrutaiConfig
from ..llm import LLMClient, LLMError, extract_json
from ..models import ChangedFile, DiffContext, Finding, Severity
from ..tools import Toolbox


class Specialist:
    name: ClassVar[str] = "specialist"
    #: One-line role description injected into the system prompt.
    role: ClassVar[str] = ""
    #: Issue categories this agent may report: slug -> description.
    categories: ClassVar[dict[str, str]] = {}
    #: Tools this agent may call (names from tools.TOOLS).
    tools: ClassVar[list[str]] = ["read_file", "grep", "git_blame"]
    #: File kinds (see ChangedFile.kind) this agent is shown.
    kinds: ClassVar[tuple[str, ...]] = ("code",)

    def __init__(self, llm: LLMClient, config: ScrutaiConfig) -> None:
        self.llm = llm
        self.config = config

    # ---- context -----------------------------------------------------------

    def files(self, diff: DiffContext) -> list[ChangedFile]:
        return [f for f in diff.files if f.kind in self.kinds and f.added]

    def seed(self, diff: DiffContext, toolbox: Toolbox) -> list[str]:
        """Observations gathered deterministically before the loop (e.g. SAST)."""
        return []

    def context(self, diff: DiffContext) -> str:
        parts = [f"ROLE: {self.name}", "TASK: review the lines this diff adds."]
        for f in self.files(diff):
            lang = f.language or "text"
            parts.append(f"=== FILE {f.path} [{lang}, {f.kind}, {f.status}]")
            parts.append(f.numbered_patch)
        return "\n".join(parts)

    def system_prompt(self, toolbox: Toolbox) -> str:
        cats = "\n".join(f"- {slug}: {desc}" for slug, desc in self.categories.items())
        return (
            f"You are the {self.name} specialist in a code review panel. {self.role}\n"
            "You work in a ReAct loop. Reply with exactly ONE JSON object per turn: either\n"
            '  {"thought": str, "action": {"tool": str, "args": {...}}}  to gather evidence, or\n'
            '  {"thought": str, "findings": [{"title": str, "body": str, "file": str, '
            '"line": int, "category": str, "severity": str, "confidence": float, '
            '"evidence": [str]}]}  when done.\n'
            f"Tools:\n{toolbox.describe()}\n"
            f"Categories (use these exact slugs):\n{cats}\n"
            "Rules: only report problems on lines the diff adds, citing their L<n> number. "
            "Evidence must quote the code or a tool observation. Prefer silence to "
            'speculation: return {"findings": []} when nothing is wrong. '
            "severity in [info, low, medium, high, critical]; confidence in [0, 1]."
        )

    # ---- the ReAct loop ----------------------------------------------------

    def review(self, diff: DiffContext) -> list[Finding]:
        if not self.files(diff):
            return []
        toolbox = Toolbox(diff.repo_root, self.tools, self.config.semgrep_config)
        system = self.system_prompt(toolbox)
        transcript = [self.context(diff)]
        for obs in self.seed(diff, toolbox):
            transcript.append(f"SEED OBSERVATION:\n{obs}")

        steps = max(self.config.max_agent_steps, 1)
        for step in range(1, steps + 1):
            if step == steps:
                transcript.append("--- FINAL: tool budget spent; reply with findings now.")
            payload = self._ask(system, "\n".join(transcript))
            if payload is None:
                return []
            if "findings" in payload:
                return self._parse(payload, diff, toolbox.calls)
            action = payload.get("action")
            if not isinstance(action, dict) or step == steps:
                return []
            tool, args = str(action.get("tool", "")), action.get("args") or {}
            obs = toolbox.run(tool, args if isinstance(args, dict) else {})
            transcript.append(
                f"--- STEP {step}\nACTION: {tool} {json.dumps(args)}\nOBSERVATION:\n{obs}"
            )
        return []

    def _ask(self, system: str, prompt: str) -> dict[str, Any] | None:
        try:
            raw = self.llm.complete(
                model=self.config.models.specialist, system=system, prompt=prompt
            )
            return extract_json(raw)
        except (LLMError, json.JSONDecodeError):
            return None

    def _parse(self, payload: dict[str, Any], diff: DiffContext, trace: list[str]) -> list[Finding]:
        items = payload.get("findings")
        if not isinstance(items, list):
            return []
        known = set(diff.paths)
        default_file = self.files(diff)[0].path
        findings: list[Finding] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            try:
                line = item.get("line")
                path = item.get("file")
                findings.append(
                    Finding(
                        agent=self.name,
                        title=str(item["title"]),
                        body=str(item.get("body", "")),
                        file=path if path in known else default_file,
                        line=int(line) if line is not None else None,
                        category=str(item.get("category") or "general").strip().lower(),
                        severity=Severity(str(item.get("severity", "medium")).lower()),
                        confidence=min(max(float(item.get("confidence", 0.5)), 0.0), 1.0),
                        evidence=[str(e) for e in item.get("evidence", []) if e] + trace,
                    )
                )
            except (TypeError, ValueError):
                continue
        return findings

    # ---- the critic debate ---------------------------------------------------

    def defend(self, finding: Finding, diff: DiffContext) -> Finding:
        """Answer the critic's challenge: gather evidence and defend, or withdraw."""
        f = finding.model_copy(deep=True)
        toolbox = Toolbox(diff.repo_root, self.tools, self.config.semgrep_config)
        system = (
            f"You are the {self.name} specialist defending a finding the critic challenged. "
            "Use tools to confirm or refute it, then reply with ONE JSON object: "
            '{"thought": str, "action": {"tool": str, "args": {...}}} to gather evidence, '
            '{"defense": str, "evidence": [str]} if it holds up, or '
            '{"withdraw": true, "reason": str} if it does not. Withdrawing a false '
            f"positive is a success, not a failure.\nTools:\n{toolbox.describe()}"
        )
        prior = "\n".join(f"- {e}" for e in f.evidence) or "none"
        transcript = [
            self.context(diff),
            "MODE: defend",
            f"FINDING: {f.title}\nCATEGORY: {f.category}\nFILE: {f.file}\nLINE: {f.line}",
            f"BODY: {f.body}\nEVIDENCE SO FAR:\n{prior}",
            f"CHALLENGE: {f.challenge}",
        ]
        for step in range(1, max(self.config.max_agent_steps, 1) + 1):
            payload = self._ask(system, "\n".join(transcript))
            if payload is None:
                break
            if payload.get("withdraw"):
                f.alive, f.contested = False, False
                reason = str(payload.get("reason", "")).strip()
                f.critic_note = f"withdrawn by {self.name}: {reason}".rstrip(": ")
                f.history.append(f"defense: withdrawn {reason}".rstrip())
                return f
            if "defense" in payload:
                f.defense = str(payload["defense"])
                new = [str(e) for e in payload.get("evidence", []) if e]
                f.evidence = [*f.evidence, *new, *toolbox.calls]
                f.history.append("defense: submitted")
                return f
            action = payload.get("action")
            if not isinstance(action, dict):
                break
            tool, args = str(action.get("tool", "")), action.get("args") or {}
            obs = toolbox.run(tool, args if isinstance(args, dict) else {})
            transcript.append(
                f"--- STEP {step}\nACTION: {tool} {json.dumps(args)}\nOBSERVATION:\n{obs}"
            )
        f.history.append("defense: none offered")
        return f
