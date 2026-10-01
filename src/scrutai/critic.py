"""The critic — Scrutai's headline mechanic.

Every finding is cross-examined: is it real, in scope, correctly rated, and
backed by evidence? The critic overwrites each finding's confidence and either
upholds, downgrades, or kills it. Findings below the configured confidence
threshold don't survive. This is the self-reflection pattern, made concrete.
"""

from __future__ import annotations

import json

from .config import ScrutaiConfig
from .llm import LLMClient
from .models import Finding

_SYSTEM = (
    "You are the critic in a code review panel. For the single finding given, "
    "decide if it is a true, in-scope issue backed by concrete evidence. "
    'Return ONLY JSON: {"confidence": float 0-1, "note": string}. '
    "Downgrade anything without a cited line or tool result."
)


def _judge(llm: LLMClient, config: ScrutaiConfig, finding: Finding) -> Finding:
    evidence = "; ".join(finding.evidence) or "none"
    prompt = (
        f"AGENT: {finding.agent}\nTITLE: {finding.title}\nBODY: {finding.body}\n"
        f"SEVERITY: {finding.severity.value}\nEVIDENCE: {evidence}"
    )
    raw = llm.complete(model=config.models.critic, system=_SYSTEM, prompt=prompt)
    try:
        verdict = json.loads(raw)
        finding.confidence = float(verdict.get("confidence", finding.confidence))
        finding.critic_note = verdict.get("note")
    except (json.JSONDecodeError, ValueError):
        finding.critic_note = "critic parse error; left as-is"
    finding.alive = finding.confidence >= config.min_confidence
    return finding


def critique(llm: LLMClient, config: ScrutaiConfig, findings: list[Finding]) -> list[Finding]:
    """Run one round of cross-examination over all findings.

    The graph may call this more than once (see orchestrator's loop edge); a
    real multi-round debate would let specialists revise and re-submit. For v0.1
    a single pass converges immediately.
    """
    return [_judge(llm, config, f) for f in findings]


def dedupe(findings: list[Finding]) -> list[Finding]:
    """Collapse the same issue raised by multiple agents, keeping the strongest."""
    best: dict[tuple[str, int | None, str], Finding] = {}
    for f in findings:
        cur = best.get(f.key())
        if cur is None or f.confidence > cur.confidence:
            best[f.key()] = f
    return list(best.values())
