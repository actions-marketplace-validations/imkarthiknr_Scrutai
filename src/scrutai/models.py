"""Core data models shared across the whole pipeline.

Everything the agents produce and the critic argues over is a typed Pydantic
object. Structured findings are what make the critic loop deterministic and the
eval harness measurable.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from .patch import DiffLine, added_lines, numbered

# Extension -> language, used by the router and by agents choosing tools.
_LANGS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".rs": "rust",
    ".php": "php",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".swift": "swift",
    ".sh": "shell",
    ".sql": "sql",
}
# Files that are never code: a change touching only these wakes no specialist.
_DOC_EXTS = {".md", ".rst", ".txt", ".adoc"}
_LOCKFILES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "Cargo.lock",
    "go.sum",
    "Gemfile.lock",
    "composer.lock",
}


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        order = ["info", "low", "medium", "high", "critical"]
        return order.index(self.value)


class Verdict(StrEnum):
    APPROVE = "approve"
    COMMENT = "comment"
    REQUEST_CHANGES = "request_changes"


class ChangedFile(BaseModel):
    path: str
    # Unified-diff hunk text for just this file. Kept raw so agents can reason
    # over exact added/removed lines.
    patch: str = ""
    # added | modified | deleted | renamed | binary
    status: str = "modified"

    @property
    def added(self) -> list[DiffLine]:
        return added_lines(self.patch)

    @property
    def numbered_patch(self) -> str:
        return numbered(self.patch)

    @property
    def suffix(self) -> str:
        name = self.path.rsplit("/", 1)[-1]
        return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""

    @property
    def language(self) -> str | None:
        return _LANGS.get(self.suffix)

    @property
    def kind(self) -> str:
        """code | test | docs | lock | other — drives routing."""
        name = self.path.rsplit("/", 1)[-1]
        if name in _LOCKFILES or self.suffix == ".lock":
            return "lock"
        if self.suffix in _DOC_EXTS:
            return "docs"
        if self.language is None:
            return "other"
        lowered = self.path.lower()
        if (
            name.startswith("test_")
            or name.endswith(
                ("_test.py", "_test.go", ".test.ts", ".test.js", ".spec.ts", ".spec.js")
            )
            or "/tests/" in f"/{lowered}"
            or "/test/" in f"/{lowered}"
            or "/__tests__/" in f"/{lowered}"
        ):
            return "test"
        return "code"


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

    agent: str  # which specialist raised it
    title: str
    body: str
    file: str
    line: int | None = None
    # Machine-readable issue type (e.g. "injection", "broad_except"). Drives
    # dedup across agents and scoring in the eval harness.
    category: str = "general"
    severity: Severity = Severity.MEDIUM
    # 0.0-1.0 confidence. Specialists propose an initial value; the critic
    # overwrites it after cross-examination.
    confidence: float = 0.5
    # Short trace of how the finding was reached (tool calls, reasoning).
    evidence: list[str] = Field(default_factory=list)
    # Populated by the critic: why it was kept, downgraded, or killed.
    critic_note: str | None = None
    alive: bool = True
    # Debate state: the critic's open question, the specialist's answer, and a
    # per-round log of the critic's decisions.
    contested: bool = False
    challenge: str | None = None
    defense: str | None = None
    history: list[str] = Field(default_factory=list)
    # True when the critic could not judge it (error/budget); such findings are dropped.
    unjudged: bool = False

    def key(self) -> tuple[str, int | None, str]:
        """Identity used for deduplication across agents.

        Two agents flagging the same category on the same line are one issue;
        uncategorised findings fall back to their title.
        """
        what = self.category if self.category != "general" else self.title.strip().lower()
        return (self.file, self.line, what)


class ReviewResult(BaseModel):
    verdict: Verdict
    findings: list[Finding]
    summary: str = ""
    # Bookkeeping the CLI / Action surface to the user.
    tokens_used: int = 0
    cost_usd: float = 0.0
    rounds: int = 0
    # Which specialists the router woke for this diff.
    agents: list[str] = Field(default_factory=list)
    # Findings the critic killed (or specialists withdrew), kept for audit
    # and for measuring how much noise the critic removes.
    dropped: list[Finding] = Field(default_factory=list)
    # The token/cost budget ran out; some agents or judgements were skipped.
    budget_exhausted: bool = False

    def by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.severity.value] = counts.get(f.severity.value, 0) + 1
        return counts
