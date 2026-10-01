from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from scrutai import review_diff
from scrutai.cli import app
from scrutai.config import ScrutaiConfig
from scrutai.llm import MockLLMClient
from scrutai.models import ChangedFile, DiffContext
from scrutai.trace import Tracer, active

DIFF = DiffContext(
    files=[
        ChangedFile(
            path="lib/a.py", patch="+try:\n+    os.system(cmd)\n+except Exception:\n+    pass\n"
        )
    ]
)


def _events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_jsonl_trace_covers_nodes_llm_tools_and_decisions(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    tracer = Tracer(jsonl=str(path))
    result = review_diff(DIFF, ScrutaiConfig(), MockLLMClient(), tracer)
    tracer.close()
    events = _events(path)
    kinds = {e["kind"] for e in events}
    assert kinds == {"node", "llm", "tool", "decision", "review", "plan", "finding", "defense"}
    nodes = [e["name"] for e in events if e["kind"] == "node" and e["phase"] == "end"]
    assert nodes[0] == "route" and nodes[-1] == "verdict" and "critic" in nodes
    final = events[-1]
    assert final["kind"] == "review" and final["kept"] == len(result.findings)
    llm_ends = [e for e in events if e["kind"] == "llm" and e["phase"] == "end"]
    assert sum(int(e["tokens"]) for e in llm_ends) == result.tokens_used  # type: ignore[call-overload]
    assert active() is None  # tracer is uninstalled after the review


def test_no_tracer_means_no_overhead_path() -> None:
    result = review_diff(DIFF, ScrutaiConfig(), MockLLMClient())
    assert result.findings


def test_cli_trace_flag(tmp_path: Path) -> None:
    path = tmp_path / "cli.jsonl"
    res = CliRunner().invoke(app, ["review", "--demo", "--trace", str(path)])
    assert res.exit_code == 1
    assert any(e["kind"] == "decision" for e in _events(path))


def test_budget_errors_are_recorded(tmp_path: Path) -> None:
    path = tmp_path / "b.jsonl"
    tracer = Tracer(jsonl=str(path))
    review_diff(DIFF, ScrutaiConfig(token_budget=300), MockLLMClient(), tracer)
    tracer.close()
    assert any("BudgetExceeded" in str(e.get("error", "")) for e in _events(path))


def test_otel_spans() -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    review_diff(DIFF, ScrutaiConfig(), MockLLMClient(), Tracer(otel=True))
    names = {s.name for s in exporter.get_finished_spans()}
    assert {"scrutai.review.diff", "scrutai.node.critic", "scrutai.llm.security"} <= names
    critic = next(s for s in exporter.get_finished_spans() if s.name == "scrutai.node.critic")
    assert critic.attributes is not None and "scrutai.ms" in critic.attributes


def test_invalid_tracing_mode_rejected() -> None:
    with pytest.raises(ValueError):
        ScrutaiConfig(tracing="carrier-pigeon")


def test_live_event_stream_tells_the_whole_story() -> None:
    events: list[dict[str, object]] = []
    tracer = Tracer(listeners=[events.append], run_id="r1")
    diff = DiffContext(
        files=[ChangedFile(path="lib/a.py", patch="+def _f(x=[]):\n+    os.system(x)\n")]
    )
    result = review_diff(diff, ScrutaiConfig(), MockLLMClient(), tracer)

    assert all(e["run"] == "r1" for e in events)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    # Every span that started also ended, with the same id.
    starts = {e["id"] for e in events if e.get("phase") == "start"}
    ends = {e["id"] for e in events if e.get("phase") == "end"}
    assert starts == ends and starts
    (plan,) = [e for e in events if e["kind"] == "plan"]
    assert {t["agent"] for t in plan["tasks"]} >= {"security", "correctness"}  # type: ignore[attr-defined]
    raised = {e["finding"] for e in events if e["kind"] == "finding"}
    decided = {e["finding"] for e in events if e["kind"] == "decision"}
    assert raised == decided  # every raised finding went on trial
    defended = [e for e in events if e["kind"] == "defense"]
    assert defended and all(e["outcome"] in ("defended", "withdrawn", "silent") for e in defended)
    kept = {f"{f.file}:{f.line}:{f.category}" for f in result.findings}
    assert kept <= raised


def test_a_broken_listener_never_breaks_a_review() -> None:
    def explode(_: dict[str, object]) -> None:
        raise RuntimeError("viewer crashed")

    result = review_diff(DIFF, ScrutaiConfig(), MockLLMClient(), Tracer(listeners=[explode]))
    assert result.findings


def test_per_call_tokens_are_exact_under_concurrency() -> None:
    events: list[dict[str, object]] = []
    result = review_diff(DIFF, ScrutaiConfig(), MockLLMClient(), Tracer(listeners=[events.append]))
    total = sum(int(e["tokens"]) for e in events if e["kind"] == "llm" and e["phase"] == "end")  # type: ignore[call-overload]
    assert total == result.tokens_used
