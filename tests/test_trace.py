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
    assert kinds == {"node", "llm", "tool", "decision", "review"}
    nodes = [e["name"] for e in events if e["kind"] == "node"]
    assert nodes[0] == "route" and nodes[-1] == "verdict" and "critic" in nodes
    final = events[-1]
    assert final["kind"] == "review" and final["kept"] == len(result.findings)
    assert sum(int(e["tokens"]) for e in events if e["kind"] == "llm") == result.tokens_used  # type: ignore[call-overload]
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
