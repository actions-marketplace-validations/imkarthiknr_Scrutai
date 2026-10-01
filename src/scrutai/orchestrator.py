"""The orchestrator graph — hierarchical delegation, built on LangGraph.

    START -> route -> specialists -> critic --(challenged & rounds left)--> defend
                                       ^                                   |
                                       +-----------------------------------+
                                       |
                                       +--(settled or out of rounds)--> verdict -> END

`route` inspects the diff and selects only relevant specialists (see router.py;
a docs-only change wakes nobody). `specialists` fans the diff out to the
selected ReAct agents. `critic` cross-examines every finding; any it
*challenges* go back to their specialist in `defend`, which gathers evidence and
defends or withdraws, and the critic re-judges. `verdict` assembles the typed
ReviewResult.
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .agents import REGISTRY
from .config import ScrutaiConfig
from .critic import critique, dedupe
from .llm import LLMClient
from .models import DiffContext, Finding, ReviewResult, Verdict
from .router import route


class ReviewState(TypedDict, total=False):
    diff: DiffContext
    selected: list[str]
    findings: list[Finding]
    round: int
    result: ReviewResult


def build_graph(llm: LLMClient, config: ScrutaiConfig) -> Any:
    def route_node(state: ReviewState) -> ReviewState:
        return {"selected": route(state["diff"], config, llm), "round": 0, "findings": []}

    def specialists_node(state: ReviewState) -> ReviewState:
        # Fan-out: run each selected specialist over the diff. (v0.2: parallelize
        # via LangGraph's Send API; sequential is correct and simpler for v0.1.)
        found: list[Finding] = []
        for name in state["selected"]:
            agent = REGISTRY[name](llm, config)
            found.extend(agent.review(state["diff"]))
        return {"findings": dedupe(found)}

    def critic_node(state: ReviewState) -> ReviewState:
        round_no = state.get("round", 0) + 1
        judged = critique(llm, config, state["findings"], state["diff"], round_no)
        return {"findings": judged, "round": round_no}

    def defend_node(state: ReviewState) -> ReviewState:
        defended: list[Finding] = []
        for f in state["findings"]:
            if f.contested and f.agent in REGISTRY:
                f = REGISTRY[f.agent](llm, config).defend(f, state["diff"])
            defended.append(f)
        return {"findings": defended}

    def _next(state: ReviewState) -> str:
        if state["round"] < config.max_critic_rounds and _unsettled(state["findings"]):
            return "defend"
        return "verdict"

    def verdict_node(state: ReviewState) -> ReviewState:
        findings = sorted(state["findings"], key=_rank)
        survivors = [f for f in findings if f.alive and f.severity.rank >= config.min_severity.rank]
        result = ReviewResult(
            verdict=_decide(survivors, config),
            findings=survivors,
            dropped=[f for f in findings if not f.alive],
            summary=_summarize(survivors, state.get("selected", [])),
            tokens_used=llm.tokens_used,
            rounds=state.get("round", 0),
            agents=state.get("selected", []),
        )
        return {"result": result}

    g = StateGraph(ReviewState)
    g.add_node("route", route_node)
    g.add_node("specialists", specialists_node)
    g.add_node("critic", critic_node)
    g.add_node("defend", defend_node)
    g.add_node("verdict", verdict_node)
    g.add_edge(START, "route")
    g.add_edge("route", "specialists")
    g.add_edge("specialists", "critic")
    g.add_conditional_edges("critic", _next, {"defend": "defend", "verdict": "verdict"})
    g.add_edge("defend", "critic")
    g.add_edge("verdict", END)
    return g.compile()


def _unsettled(findings: list[Finding]) -> bool:
    """The debate continues while the critic has an open challenge."""
    return any(f.contested for f in findings)


def _rank(f: Finding) -> tuple[int, float, str, int]:
    return (-f.severity.rank, -f.confidence, f.file, f.line or 0)


def _decide(findings: list[Finding], config: ScrutaiConfig) -> Verdict:
    if any(f.severity.rank >= config.fail_on.rank for f in findings):
        return Verdict.REQUEST_CHANGES
    return Verdict.COMMENT if findings else Verdict.APPROVE


def _summarize(findings: list[Finding], agents: list[str]) -> str:
    if not agents:
        return "No reviewable code changed (docs, lockfiles or assets only). Nothing to review."
    if not findings:
        return "No issues survived cross-examination. Looks clean."
    top = findings[0]
    return f"{len(findings)} issue(s) upheld. Most severe: {top.title} ({top.severity.value})."


def review_diff(diff: DiffContext, config: ScrutaiConfig, llm: LLMClient) -> ReviewResult:
    """Convenience entry point used by the CLI and the eval harness."""
    graph = build_graph(llm, config)
    final = graph.invoke({"diff": diff})
    result: ReviewResult = final["result"]
    return result
