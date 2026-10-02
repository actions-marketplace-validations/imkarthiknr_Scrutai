"""The critic — Scrutai's headline mechanic.

Every finding is cross-examined: is it real, in scope, correctly rated, and
backed by evidence? The critic sees the cited code with its surrounding lines
(not just the specialist's claim) and returns one of four decisions:

    uphold     - the finding stands; confidence is set by the critic
    downgrade  - real, but over-rated; severity is lowered
    kill       - false positive / out of scope / unsupported
    challenge  - plausible but under-evidenced; the specialist must defend it

A challenge starts a debate round: the originating specialist gets the
critic's question, may gather fresh tool evidence, and either defends or
withdraws; the critic then re-judges. `max_critic_rounds` bounds the debate.
Anything still contested when rounds run out is judged on the critic's
provisional confidence. This is the self-reflection pattern, made concrete.
"""

from __future__ import annotations

import json
from typing import Any

from .concurrency import parallel_map
from .config import ScrutaiConfig
from .llm import LLMClient, LLMError, extract_json
from .models import DiffContext, Finding, Severity
from .patch import new_file_lines, window
from .trace import emit, finding_ref

SYSTEM = (
    "You are the critic in a code review panel. You receive ONE finding from a "
    "specialist plus the code it cites. Be adversarial: specialists over-report. "
    "Kill it if the cited line is not added by this diff, if the pattern only appears "
    "in a comment or string, if the value is a placeholder or test fixture, or if "
    "nothing untrusted can reach the sink. Downgrade it if real but over-rated. "
    "Challenge it (with a concrete question) if it is plausible but no tool "
    "observation backs it. Uphold it only if the evidence shows a real problem.\n"
    'Return ONLY JSON: {"decision": "uphold"|"downgrade"|"kill"|"challenge", '
    '"confidence": float 0-1, "severity"?: str, "note": str, "question"?: str}'
)


def build_prompt(finding: Finding, diff: DiffContext, round_no: int, max_rounds: int) -> str:
    changed = next((f for f in diff.files if f.path == finding.file), None)
    cited = "(no line cited)"
    context = "(file not in diff)"
    kind = changed.kind if changed else "unknown"
    if changed is not None and finding.line is not None:
        text, added = new_file_lines(changed.patch).get(finding.line, ("", False))
        cited = text if added else f"(line {finding.line} is not added by this diff)"
        context = window(changed.patch, finding.line) or "(no context)"
    evidence = "\n".join(f"- {e}" for e in finding.evidence) or "none"
    parts = [
        "ROLE: critic",
        f"ROUND: {round_no} of {max_rounds}",
        f"AGENT: {finding.agent}",
        f"CATEGORY: {finding.category}",
        f"TITLE: {finding.title}",
        f"BODY: {finding.body}",
        f"SEVERITY: {finding.severity.value}",
        f"CLAIMED CONFIDENCE: {finding.confidence:.2f}",
        f"FILE: {finding.file} (kind: {kind})",
        f"LINE: {finding.line}",
        f"CITED CODE: {cited}",
        f"CONTEXT:\n{context}",
        f"EVIDENCE:\n{evidence}",
    ]
    if finding.challenge:
        parts.append(f"YOUR EARLIER CHALLENGE: {finding.challenge}")
    if finding.defense:
        parts.append(f"SPECIALIST DEFENSE: {finding.defense}")
    return "\n".join(parts)


def judge(
    llm: LLMClient,
    config: ScrutaiConfig,
    finding: Finding,
    diff: DiffContext,
    round_no: int,
) -> Finding:
    """Cross-examine one finding and return its updated copy."""
    f = finding.model_copy(deep=True)
    prompt = build_prompt(f, diff, round_no, config.max_critic_rounds)
    verdict: dict[str, Any] = {}
    try:
        raw = llm.complete(model=config.models.critic, system=SYSTEM, prompt=prompt)
        verdict = extract_json(raw)
        decision = str(verdict.get("decision", "uphold")).lower()
        f.confidence = min(max(float(verdict.get("confidence", f.confidence)), 0.0), 1.0)
        note = str(verdict.get("note", "")).strip()
    except (LLMError, json.JSONDecodeError, TypeError, ValueError) as exc:
        # Every finding survives scrutiny or it doesn't ship: one the critic
        # could not judge (provider error, garbage reply, budget spent) is
        # withheld, never passed through on the specialist's word alone.
        f.alive = f.contested = False
        f.unjudged = True
        f.critic_note = f"not cross-examined ({type(exc).__name__}); withheld"
        f.history.append(f"round {round_no}: unjudged {exc!s}"[:200])
        return f

    f.critic_note = note or decision
    f.history.append(f"round {round_no}: {decision} ({f.confidence:.2f}) {note}".rstrip())
    emit("decision", round=round_no, decision=decision, note=note, **finding_ref(f))
    f.contested = False
    if decision == "kill":
        f.alive = False
        return f
    if decision == "downgrade":
        try:
            lowered = Severity(str(verdict.get("severity", "")).lower())
        except ValueError:
            lowered = f.severity
        if lowered.rank < f.severity.rank:
            f.severity = lowered
    elif decision == "challenge":
        f.contested = True
        f.challenge = str(verdict.get("question") or note or "Provide tool evidence.")
    f.alive = f.confidence >= config.min_confidence
    return f


def critique(
    llm: LLMClient,
    config: ScrutaiConfig,
    findings: list[Finding],
    diff: DiffContext,
    round_no: int = 1,
) -> list[Finding]:
    """Run one round of cross-examination.

    Round 1 judges every finding; later rounds re-judge only the ones that were
    challenged and defended (withdrawn findings are already dead).
    """

    def one(f: Finding) -> Finding:
        return judge(llm, config, f, diff, round_no) if round_no == 1 or f.contested else f

    return parallel_map(one, findings, config.concurrency)


def dedupe(findings: list[Finding]) -> list[Finding]:
    """Collapse the same issue raised by multiple agents, keeping the strongest."""
    best: dict[tuple[str, int | None, str], Finding] = {}
    for f in findings:
        cur = best.get(f.key())
        if cur is None or f.confidence > cur.confidence:
            best[f.key()] = f
    return list(best.values())
