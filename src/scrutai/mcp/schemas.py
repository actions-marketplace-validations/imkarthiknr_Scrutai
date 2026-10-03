"""Structured tool output: what an MCP client gets back from a review.

Finding text comes from the reviewed code and from model output. Clients must
treat it as data, never as instructions; tool descriptions say so too.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models import Finding, ReviewResult

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
    def of(cls, finding: Finding, index: int) -> FindingOut:
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
            evidence=[_clip(e) for e in finding.evidence[:MAX_EVIDENCE_ITEMS]],
            critic_note=finding.critic_note,
            defended=finding.defense is not None,
        )


class ReviewSummary(BaseModel):
    """The outcome of one review."""

    review_id: str
    label: str = Field(description="What was reviewed, e.g. 'main...HEAD'.")
    verdict: str = Field(description="approve | comment | request_changes")
    summary: str
    findings: list[FindingOut]
    dropped: int = Field(description="Findings the critic killed or specialists withdrew.")
    agents: list[str] = Field(description="Specialists the router woke.")
    files: int = Field(description="Files in the reviewed diff, after include/exclude filters.")
    partial: bool = Field(description="Budget ran out or the review was cancelled.")
    budget_exhausted: bool
    cancelled: bool
    rounds: int
    tokens_used: int
    cost_usd: float

    @classmethod
    def of(cls, review_id: str, label: str, result: ReviewResult, files: int) -> ReviewSummary:
        return cls(
            review_id=review_id,
            label=label,
            verdict=result.verdict.value,
            summary=result.summary,
            findings=[FindingOut.of(f, i) for i, f in enumerate(result.findings, 1)],
            dropped=len(result.dropped),
            agents=result.agents,
            files=files,
            partial=result.budget_exhausted or result.cancelled,
            budget_exhausted=result.budget_exhausted,
            cancelled=result.cancelled,
            rounds=result.rounds,
            tokens_used=result.tokens_used,
            cost_usd=round(result.cost_usd, 6),
        )
