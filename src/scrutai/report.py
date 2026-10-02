"""Render a ReviewResult for humans (Markdown) and for tools (SARIF 2.1.0).

The Markdown renderer is shared by the CLI and the GitHub Action, so a PR
comment and a local run read the same.
"""

from __future__ import annotations

import json
from typing import Any

from . import __version__
from .models import Finding, ReviewResult, Severity

_ICON = {
    Severity.CRITICAL: "🛑",
    Severity.HIGH: "🔴",
    Severity.MEDIUM: "🟠",
    Severity.LOW: "🟡",
    Severity.INFO: "🔵",
}
_SARIF_LEVEL = {
    Severity.CRITICAL: "error",
    Severity.HIGH: "error",
    Severity.MEDIUM: "warning",
    Severity.LOW: "note",
    Severity.INFO: "note",
}


def location(f: Finding) -> str:
    return f"{f.file}:{f.line}" if f.line else f.file


def finding_markdown(f: Finding) -> str:
    """One finding as a self-contained block (also used as an inline PR comment)."""
    lines = [
        f"{_ICON[f.severity]} **{f.title}** · `{f.severity.value}` · {f.agent} · "
        f"confidence {f.confidence:.2f}",
        "",
        f.body,
    ]
    if f.critic_note:
        lines += ["", f"_Critic:_ {f.critic_note}"]
    evidence = [e for e in f.evidence if not e.startswith("tool:")]
    if evidence:
        lines += ["", "<details><summary>Evidence</summary>", ""]
        lines += [f"- `{e}`" if len(e) < 160 else f"- {e}" for e in evidence]
        lines += ["", "</details>"]
    return "\n".join(lines)


def to_markdown(result: ReviewResult, show_dropped: bool = False) -> str:
    head = [
        f"### Scrutai review: `{result.verdict.value}`",
        "",
        result.summary,
        "",
        f"<sub>agents: {', '.join(result.agents) or 'none'} · critic rounds: {result.rounds} · "
        f"tokens: {result.tokens_used} · cost: ${result.cost_usd:.4f} · "
        f"dropped by critic: {len(result.dropped)}</sub>",
    ]
    if result.budget_exhausted:
        head += ["", "> ⚠️ Budget exhausted: this review is partial."]
    if result.findings:
        head += [
            "",
            "| | Severity | Location | Finding | Agent | Conf |",
            "|---|---|---|---|---|---|",
        ]
        for f in result.findings:
            head.append(
                f"| {_ICON[f.severity]} | {f.severity.value} | `{location(f)}` | "
                f"{f.title} | {f.agent} | {f.confidence:.2f} |"
            )
        for f in result.findings:
            head += ["", "---", "", f"`{location(f)}`", "", finding_markdown(f)]
    if show_dropped and result.dropped:
        head += ["", "<details><summary>Dropped by the critic</summary>", ""]
        for f in result.dropped:
            head.append(f"- `{location(f)}` {f.title} ({f.agent}): {f.critic_note}")
        head += ["", "</details>"]
    return "\n".join(head) + "\n"


def to_sarif(result: ReviewResult) -> dict[str, Any]:
    rules: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for f in result.findings:
        rules.setdefault(
            f.category,
            {
                "id": f.category,
                "name": f.category,
                "shortDescription": {"text": f.title},
                "properties": {"agent": f.agent},
            },
        )
        loc: dict[str, Any] = {"artifactLocation": {"uri": f.file}}
        if f.line:
            loc["region"] = {"startLine": f.line}
        results.append(
            {
                "ruleId": f.category,
                "level": _SARIF_LEVEL[f.severity],
                "message": {"text": f"{f.title}: {f.body}".strip(": ")},
                "locations": [{"physicalLocation": loc}],
                "partialFingerprints": {"scrutai/v1": f.fingerprint()},
                "properties": {
                    "agent": f.agent,
                    "severity": f.severity.value,
                    "confidence": round(f.confidence, 3),
                    "critic": f.critic_note or "",
                },
            }
        )
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "Scrutai",
                        "version": __version__,
                        "informationUri": "https://github.com/imkarthiknr/Scrutai",
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def to_sarif_json(result: ReviewResult) -> str:
    return json.dumps(to_sarif(result), indent=2)
