"""The orchestrator graph — hierarchical delegation, built on LangGraph.

    START -> route -> specialists -> critic --(unstable & rounds left)--> critic
                                       |
                                       +--(stable)--> verdict -> END

`route` inspects the diff and selects only relevant specialists (smart routing
= a cost lever and a sign of judgement). `specialists` fans the diff out to the
selected ReAct agents. `critic` cross-examines their findings and can loop.
`verdict` assembles the typed ReviewResult.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .agents import REGISTRY
from .config import ScrutaiConfig
from .critic import critique, dedupe
from .llm import LLMClient
from .models import DiffContext, Finding, ReviewResult, Severity, Verdict


class ReviewState(TypedDict, total=False):
    diff: DiffContext
    selected: list[str]
    findings: list[Finding]
    round: int
    result: ReviewResult


def _route(diff: DiffContext, config: ScrutaiConfig) -> list[str]:
    """Pick specialists worth running for this diff. Cheap heuristics now; an
    LLM router later. A docs-only or lockfile-only change wakes nobody."""
    blob = "\n".join(f.patch for f in diff.files).lower()
    selected: list[str] = []
    for name in config.enabled_agents:
        if name == "security" and not any(
            s in blob for s in ("eval(", "subprocess", "os.system", "pickle", "token", "password")
        ):
            continue
        selected.append(name)
    return selected or list(config.enabled_agents)


def build_graph(llm: LLMClient, config: ScrutaiConfig):
    def route_node(state: ReviewState) -> ReviewState:
        return {"selected": _route(state["diff"], config), "round": 0, "findings": []}

    def specialists_node(state: ReviewState) -> ReviewState:
        # Fan-out: run each selected specialist over the diff. (v0.2: parallelize
        # via LangGraph's Send API; sequential is correct and simpler for v0.1.)
        found: list[Finding] = []
        for name in state["selected"]:
            agent = REGISTRY[name](llm, config)
            found.extend(agent.review(state["diff"]))
        return {"findings": dedupe(found)}

    def critic_node(state: ReviewState) -> ReviewState:
        judged = critique(llm, config, state["findings"])
        return {"findings": judged, "round": state.get("round", 0) + 1}

    def _needs_another_round(state: ReviewState) -> str:
        # Hook for real multi-round debate; converges immediately in v0.1.
        if state["round"] < config.max_critic_rounds and _unstable(state["findings"]):
            return "critic"
        return "verdict"

    def verdict_node(state: ReviewState) -> ReviewState:
        survivors = [
            f
            for f in state["findings"]
            if f.alive and f.severity.rank >= config.min_severity.rank
        ]
        result = ReviewResult(
            verdict=_decide(survivors, config),
            findings=sorted(survivors, key=lambda f: -f.severity.rank),
            summary=_summarize(survivors),
            tokens_used=llm.tokens_used,
            rounds=state.get("round", 0),
        )
        return {"result": result}

    g = StateGraph(ReviewState)
    g.add_node("route", route_node)
    g.add_node("specialists", specialists_node)
    g.add_node("critic", critic_node)
    g.add_node("verdict", verdict_node)
    g.add_edge(START, "route")
    g.add_edge("route", "specialists")
    g.add_edge("specialists", "critic")
    g.add_conditional_edges("critic", _needs_another_round, {"critic": "critic", "verdict": "verdict"})
    g.add_edge("verdict", END)
    return g.compile()


def _unstable(findings: list[Finding]) -> bool:
    # Placeholder for a real convergence check (e.g. a finding flipped state).
    return False


def _decide(findings: list[Finding], config: ScrutaiConfig) -> Verdict:
    if any(f.severity.rank >= config.fail_on.rank for f in findings):
        return Verdict.REQUEST_CHANGES
    return Verdict.COMMENT if findings else Verdict.APPROVE


def _summarize(findings: list[Finding]) -> str:
    if not findings:
        return "No issues survived cross-examination. Looks clean."
    top = findings[0]
    return f"{len(findings)} issue(s) upheld. Most severe: {top.title} ({top.severity.value})."


def review_diff(diff: DiffContext, config: ScrutaiConfig, llm: LLMClient) -> ReviewResult:
    """Convenience entry point used by the CLI and the eval harness."""
    graph = build_graph(llm, config)
    final = graph.invoke({"diff": diff})
    return final["result"]
