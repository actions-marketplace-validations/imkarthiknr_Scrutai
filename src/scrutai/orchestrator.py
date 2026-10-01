"""The orchestrator graph — hierarchical delegation, built on LangGraph.

                  +-> specialist (security) -+
    START -> route -+-> specialist (tests)    -+-> collect -> critic --(challenged)--> defend
                    +-> specialist (...)      -+                ^  |                     |
                                                                |  +---------------------+
                                                                |
                                                 (settled or out of rounds) -> verdict -> END

`route` inspects the diff and selects only relevant specialists (see router.py;
a docs-only change wakes nobody). `specialists` fans the diff out to the
selected ReAct agents, one concurrent branch each (LangGraph Send), and
`collect` merges and dedupes their findings. `critic` cross-examines every finding; any it
*challenges* go back to their specialist in `defend`, which gathers evidence and
defends or withdraws, and the critic re-judges. `verdict` assembles the typed
ReviewResult.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .agents import REGISTRY
from .concurrency import parallel_map
from .config import ScrutaiConfig
from .critic import critique, dedupe
from .diff import chunk_diff
from .llm import BudgetedClient, LLMClient
from .models import DiffContext, Finding, ReviewResult, Verdict
from .router import heuristic_route, route
from .trace import Tracer, TracingClient, span, traced_node, tracing


class ReviewState(TypedDict, total=False):
    diff: DiffContext
    selected: list[str]
    # Every specialist branch appends here concurrently; the reducer merges them.
    raw: Annotated[list[Finding], operator.add]
    findings: list[Finding]
    round: int
    result: ReviewResult


class SpecialistTask(TypedDict):
    diff: DiffContext
    agent: str


def build_graph(llm: LLMClient, config: ScrutaiConfig) -> Any:
    def route_node(state: ReviewState) -> ReviewState:
        return {"selected": route(state["diff"], config, llm), "round": 0}

    def fan_out(state: ReviewState) -> list[Send] | str:
        # One branch per (specialist, chunk); LangGraph runs them concurrently.
        # Each chunk is routed on its own, so a chunk without risky code never
        # wakes the security agent even when another chunk did.
        sends = [
            Send("specialist", {"diff": chunk, "agent": name})
            for chunk in chunk_diff(state["diff"], config.chunk_lines)
            for name in heuristic_route(chunk, config)
            if name in state["selected"]
        ]
        return sends or "collect"

    def specialist_node(task: SpecialistTask) -> ReviewState:
        with span("node", "specialist", agent=task["agent"], files=task["diff"].paths) as extra:
            found = REGISTRY[task["agent"]](llm, config).review(task["diff"])
            extra["findings"] = len(found)
        return {"raw": found}

    def collect_node(state: ReviewState) -> ReviewState:
        # Branches finish in any order: sort before dedup so ties break the same way.
        raw = sorted(state.get("raw", []), key=lambda f: (f.file, f.line or 0, f.category, f.agent))
        return {"findings": dedupe(raw)}

    def critic_node(state: ReviewState) -> ReviewState:
        round_no = state.get("round", 0) + 1
        judged = critique(llm, config, state["findings"], state["diff"], round_no)
        return {"findings": judged, "round": round_no}

    def defend_node(state: ReviewState) -> ReviewState:
        def defend(f: Finding) -> Finding:
            if f.contested and f.agent in REGISTRY:
                return REGISTRY[f.agent](llm, config).defend(f, state["diff"])
            return f

        return {"findings": parallel_map(defend, state["findings"], config.concurrency)}

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
            cost_usd=round(llm.cost_usd, 6),
            rounds=state.get("round", 0),
            agents=state.get("selected", []),
        )
        return {"result": result}

    g = StateGraph(ReviewState)
    g.add_node("route", traced_node("route", route_node))
    # Send-target nodes take their own input type, which LangGraph's stubs can't express.
    g.add_node("specialist", cast(Any, specialist_node))
    g.add_node("collect", traced_node("collect", collect_node))
    g.add_node("critic", traced_node("critic", critic_node))
    g.add_node("defend", traced_node("defend", defend_node))
    g.add_node("verdict", traced_node("verdict", verdict_node))
    g.add_edge(START, "route")
    g.add_conditional_edges("route", fan_out, ["specialist", "collect"])
    g.add_edge("specialist", "collect")
    g.add_edge("collect", "critic")
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


def review_diff(
    diff: DiffContext, config: ScrutaiConfig, llm: LLMClient, tracer: Tracer | None = None
) -> ReviewResult:
    """Review one diff. The entry point the CLI, the Action and the eval harness share."""
    budgeted = BudgetedClient(llm, config.token_budget, config.max_cost_usd)
    client: LLMClient = TracingClient(budgeted) if tracer is not None else budgeted
    with tracing(tracer), span("review", "diff", files=len(diff.files)) as extra:
        graph = build_graph(client, config)
        final = graph.invoke({"diff": diff})
        result: ReviewResult = final["result"]
        extra.update(
            verdict=result.verdict.value,
            kept=len(result.findings),
            dropped=len(result.dropped),
            tokens=budgeted.tokens_used,
        )
    if budgeted.exhausted:
        result.budget_exhausted = True
        result.summary += (
            f" Budget exhausted ({budgeted.tokens_used} tokens): this review is partial."
        )
    return result
