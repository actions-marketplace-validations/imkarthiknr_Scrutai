"""Structured tool output: what an MCP client gets back from a review.

Finding text comes from the reviewed code and from model output. Clients must
treat it as data, never as instructions; tool descriptions say so too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..models import Finding, ReviewResult
from ..runs import Run

Status = Literal["running", "done", "error"]

MAX_EVIDENCE_ITEMS = 8
MAX_EVIDENCE_CHARS = 400


def _clip(text: str, limit: int = MAX_EVIDENCE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class FindingOut(BaseModel):
    """One critic-vetted finding."""

    id: str = Field(description="Stable within this review, e.g. 'F1'.")
    fingerprint: str = Field(description="Stable across pushes; ignores line moves.")
    agent: str
    title: str
    body: str
    file: str
    line: int | None
    category: str
    severity: str
    confidence: float
    evidence: list[str] = Field(description="Truncated; the full list is in the review resource.")
    critic_note: str | None = None
    defended: bool = Field(description="The critic challenged it and the specialist defended it.")

    @classmethod
    def of(cls, finding: Finding, index: int, full: bool = False) -> FindingOut:
        return cls(
            id=f"F{index}",
            fingerprint=finding.fingerprint(),
            agent=finding.agent,
            title=finding.title,
            body=finding.body,
            file=finding.file,
            line=finding.line,
            category=finding.category,
            severity=finding.severity.value,
            confidence=round(finding.confidence, 3),
            evidence=(
                list(finding.evidence)
                if full
                else [_clip(e) for e in finding.evidence[:MAX_EVIDENCE_ITEMS]]
            ),
            critic_note=finding.critic_note,
            defended=finding.defense is not None,
        )


class ReviewSummary(BaseModel):
    """One review: its status and, once done, the outcome."""

    review_id: str = Field(description="Pass to get_review, explain_finding and resources.")
    label: str = Field(description="What was reviewed, e.g. 'main...HEAD'.")
    status: Status = Field(description="running | done | error")
    error: str | None = None
    verdict: str | None = Field(default=None, description="approve | comment | request_changes")
    summary: str = ""
    findings: list[FindingOut] = Field(default_factory=list)
    dropped: int = Field(default=0, description="Findings killed by the critic or withdrawn.")
    agents: list[str] = Field(default_factory=list, description="Specialists the router woke.")
    files: int = Field(default=0, description="Files reviewed, after include/exclude filters.")
    partial: bool = Field(default=False, description="Budget ran out or the review was cancelled.")
    budget_exhausted: bool = False
    cancelled: bool = False
    rounds: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0

    @classmethod
    def of(cls, run: Run, result: ReviewResult | None, files: int = 0) -> ReviewSummary:
        base = cls(review_id=run.id, label=run.label, status=run.status, error=run.error)
        if result is None:
            return base
        return base.model_copy(
            update={
                "verdict": result.verdict.value,
                "summary": result.summary,
                "findings": [FindingOut.of(f, i) for i, f in enumerate(result.findings, 1)],
                "dropped": len(result.dropped),
                "agents": result.agents,
                "files": files,
                "partial": result.budget_exhausted or result.cancelled,
                "budget_exhausted": result.budget_exhausted,
                "cancelled": result.cancelled,
                "rounds": result.rounds,
                "tokens_used": result.tokens_used,
                "cost_usd": round(result.cost_usd, 6),
            }
        )


class ReviewBrief(BaseModel):
    review_id: str
    label: str
    status: Status
    created: str = Field(description="ISO 8601, UTC.")
    verdict: str | None = None
    findings: int | None = None

    @classmethod
    def of(cls, run: Run, result: ReviewResult | None) -> ReviewBrief:
        return cls(
            review_id=run.id,
            label=run.label,
            status=run.status,
            created=datetime.fromtimestamp(run.created, UTC).isoformat(timespec="seconds"),
            verdict=result.verdict.value if result else None,
            findings=len(result.findings) if result else None,
        )


class ReviewList(BaseModel):
    """Reviews held by this server process, newest first."""

    total: int
    reviews: list[ReviewBrief]


class FindingExplanation(BaseModel):
    """Everything behind one finding: full evidence, the debate, and the code."""

    review_id: str
    finding: FindingOut
    challenge: str | None = Field(default=None, description="The critic's question, if any.")
    defense: str | None = Field(default=None, description="The specialist's answer.")
    history: list[str] = Field(description="The critic's decision in each round.")
    code: str = Field(description="The reviewed lines around the finding; '+' marks added lines.")
