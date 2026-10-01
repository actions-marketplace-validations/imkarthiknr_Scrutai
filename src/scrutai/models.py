"""Core data models shared across the whole pipeline.

Everything the agents produce and the critic argues over is a typed Pydantic
object. Structured findings are what make the critic loop deterministic and the
eval harness measurable.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        order = ["info", "low", "medium", "high", "critical"]
        return order.index(self.value)


class Verdict(str, Enum):
    APPROVE = "approve"
    COMMENT = "comment"
    REQUEST_CHANGES = "request_changes"


class ChangedFile(BaseModel):
    path: str
    # Unified-diff hunk text for just this file. Kept raw so agents can reason
    # over exact added/removed lines.
    patch: str = ""


class DiffContext(BaseModel):
    """The unit of work handed to the orchestrator: one PR / one diff."""

    repo_root: str = "."
    base_ref: str = "main"
    head_ref: str = "HEAD"
    files: list[ChangedFile] = Field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]


class Finding(BaseModel):
    """A single issue raised by a specialist and judged by the critic."""

    agent: str                      # which specialist raised it
    title: str
    body: str
    file: str
    line: int | None = None
    severity: Severity = Severity.MEDIUM
    # 0.0-1.0 confidence. Specialists propose an initial value; the critic
    # overwrites it after cross-examination.
    confidence: float = 0.5
    # Short trace of how the finding was reached (tool calls, reasoning).
    evidence: list[str] = Field(default_factory=list)
    # Populated by the critic: why it was kept, downgraded, or killed.
    critic_note: str | None = None
    alive: bool = True

    def key(self) -> tuple[str, str, int | None]:
        """Identity used for deduplication across agents."""
        return (self.file, self.title.strip().lower(), self.line)


class ReviewResult(BaseModel):
    verdict: Verdict
    findings: list[Finding]
    summary: str = ""
    # Bookkeeping the CLI / Action surface to the user.
    tokens_used: int = 0
    cost_usd: float = 0.0
    rounds: int = 0

    def by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.severity.value] = counts.get(f.severity.value, 0) + 1
        return counts
