"""Routing — decide which specialists are worth waking for a diff.

Routing is the main cost lever: every specialist skipped is a ReAct loop (and a
critic pass over its findings) not paid for. Heuristics run first and are
always on; an optional LLM router may *narrow* that set further but can never
add an agent the heuristics ruled out, so a confused router can only save
money, never widen the blast radius.
"""

from __future__ import annotations

import json
import re

from .config import ScrutaiConfig
from .llm import LLMClient, LLMError, extract_json
from .models import DiffContext

# Added code touching any of these is worth a security look.
_RISK_SURFACE = re.compile(
    r"eval\(|exec\(|subprocess|os\.system|popen|pickle|marshal|yaml\.load|"
    r"execute\(|raw\(|\bsql\b|query|password|passwd|secret|token|api[_-]?key|"
    r"private[_-]?key|auth|login|session|cookie|jwt|crypt|hash|md5|sha1|random|"
    r"verify\s*=|ssl|tls|request|http|urllib|open\(|innerhtml|dangerouslyset|"
    r"cors|csrf|chmod|tempfile|mktemp|deserial",
    re.IGNORECASE,
)
_SENSITIVE_PATH = re.compile(r"auth|security|crypto|login|secret|permission|token", re.I)
_DEF = re.compile(r"^\s*(?:async\s+)?(?:def|function|func|fn)\s+\w+|=>\s*[{(]?")
# Patterns a performance reviewer cares about: loops and expensive calls.
_PERF_SURFACE = re.compile(
    r"\bfor\b|\bwhile\b|\.execute\(|\.query\(|requests\.|fetch\(|\.filter\(|"
    r"\.all\(\)|re\.compile|sorted\(|\.sort\(|\bin\s+\w+\s*:",
)


def heuristic_route(diff: DiffContext, config: ScrutaiConfig) -> list[str]:
    """Cheap, deterministic routing. Order follows `config.enabled_agents`."""
    code = [f for f in diff.files if f.kind == "code"]
    reviewable = [f for f in diff.files if f.kind in ("code", "test")]
    if not reviewable:
        return []  # docs-only, lockfile-only, or asset-only: wake nobody

    added_code = "\n".join(d.text for f in code for d in f.added)
    added_any = "\n".join(d.text for f in reviewable for d in f.added)

    wants: dict[str, bool] = {
        "security": bool(_RISK_SURFACE.search(added_any))
        or any(_SENSITIVE_PATH.search(f.path) for f in reviewable),
        "correctness": bool(added_code.strip()),
        "tests": any(_DEF.search(d.text) for f in code for d in f.added),
        "performance": bool(_PERF_SURFACE.search(added_code)),
        "style": bool(added_any.strip()),
    }
    return [name for name in config.enabled_agents if wants.get(name, True)]


_ROUTER_SYSTEM = (
    "You are the router of a code review panel. Given a diff summary and a list of "
    'candidate specialists, return ONLY JSON {"agents": [names]} with the subset '
    "worth running. Drop a specialist only if the diff clearly cannot contain issues "
    "in its area."
)


def llm_route(
    diff: DiffContext, config: ScrutaiConfig, llm: LLMClient, candidates: list[str]
) -> list[str]:
    """Ask the cheap router model to narrow `candidates`. Falls back to them on error."""
    if len(candidates) <= 1:
        return candidates
    summary = "\n".join(
        f"{f.path} ({f.kind}, +{len(f.added)} lines)\n" + "\n".join(d.text for d in f.added[:15])
        for f in diff.files
    )
    prompt = f"ROLE: router\nCANDIDATES: {', '.join(candidates)}\nDIFF:\n{summary}"
    try:
        raw = llm.complete(model=config.models.router, system=_ROUTER_SYSTEM, prompt=prompt)
        picked = extract_json(raw).get("agents", candidates)
    except (LLMError, json.JSONDecodeError, AttributeError):
        return candidates
    if not isinstance(picked, list):
        return candidates
    # Narrow only: never add an agent the heuristics ruled out.
    return [c for c in candidates if c in picked]


def route(diff: DiffContext, config: ScrutaiConfig, llm: LLMClient) -> list[str]:
    selected = heuristic_route(diff, config)
    if config.routing == "llm":
        selected = llm_route(diff, config, llm, selected)
    return selected
