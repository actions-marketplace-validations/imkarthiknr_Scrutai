"""run_benchmark over MCP."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from mcp_util import in_memory, is_error, structured, text

from scrutai.eval.harness import BUNDLED_CASES
from scrutai.mcp.server import Settings, build_server

pytestmark = pytest.mark.anyio

CASES = BUNDLED_CASES


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make(tmp_path: Path, config: str = "", **kw: Any) -> Any:
    cfg = tmp_path / "scrutai.yml"
    cfg.write_text(config)
    kw.setdefault("benchmark", str(CASES))
    return build_server(Settings(roots=[str(tmp_path)], config_path=str(cfg), **kw))


async def test_benchmark_reports_metrics_with_progress(tmp_path: Path) -> None:
    progress: list[tuple[float, float | None, str | None]] = []

    async def on_progress(done: float, total: float | None, message: str | None) -> None:
        progress.append((done, total, message))

    async with in_memory(make(tmp_path)) as client:
        result = await client.call_tool(
            "run_benchmark", {"limit": 4}, progress_callback=on_progress
        )
    data = structured(result)
    assert data["status"] == "done" and data["llm_mode"] == "mock" and data["cases"] == 4
    assert data["precision"] == 1.0 and data["recall"] is not None
    assert data["report_markdown"].startswith("# Scrutai benchmark report")
    assert data["backends"] is None and data["agreement"] is None
    assert [p[:2] for p in progress] == [(1, 4), (2, 4), (3, 4), (4, 4)]
    assert progress[0][2] == "Case inj-01"


async def test_full_benchmark_matches_the_ci_gate(tmp_path: Path) -> None:
    async with in_memory(make(tmp_path)) as client:
        data = structured(await client.call_tool("run_benchmark", {}))
    assert data["cases"] == 50
    assert data["precision"] >= 0.95 and data["recall"] >= 0.8
    assert data["critic_precision_lift"] > 0  # the critic earns its keep
    assert "injection" in data["per_category"]
    assert any(m.startswith("miss-") for m in data["misses"])  # the labelled expected misses


@pytest.mark.skipif(importlib.util.find_spec("crewai") is None, reason="needs scrutai[crewai]")
async def test_compare_backends(tmp_path: Path) -> None:
    async with in_memory(make(tmp_path)) as client:
        data = structured(
            await client.call_tool("run_benchmark", {"compare_backend": "crewai", "limit": 2})
        )
    assert set(data["backends"]) == {"native", "crewai"} and data["agreement"] == 1.0
    assert data["report_markdown"].startswith("# Framework comparison")


async def test_bad_arguments(tmp_path: Path) -> None:
    async with in_memory(make(tmp_path)) as client:
        backend = await client.call_tool("run_benchmark", {"compare_backend": "native"})
        limit = await client.call_tool("run_benchmark", {"limit": 0})
    assert is_error(backend) and "compare_backend must be one of" in text(backend)
    assert is_error(limit)


async def test_missing_benchmark_file(tmp_path: Path) -> None:
    async with in_memory(make(tmp_path, benchmark=str(tmp_path / "none.jsonl"))) as client:
        result = await client.call_tool("run_benchmark", {})
    assert is_error(result) and "cannot read the benchmark" in text(result)


async def test_live_runs_need_the_users_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_model(mode: str) -> Any:
        raise AssertionError("a declined benchmark must not call the model")

    monkeypatch.setattr("scrutai.eval.harness.make_client", no_model)
    asked: list[str] = []

    def decline(message: str) -> None:
        asked.append(message)
        return None

    srv = make(tmp_path, "llm_mode: live\n")
    async with in_memory(srv, elicit=decline) as client:
        declined = structured(await client.call_tool("run_benchmark", {"limit": 3}))
    async with in_memory(srv) as client:  # cannot ask, and no confirm=true
        refused = await client.call_tool("run_benchmark", {"limit": 3})
    assert declined["status"] == "declined" and declined["cases"] == 0
    assert asked == [
        "Run 3 benchmark review(s) against the live model (live)? Each calls the model "
        "and costs money."
    ]
    assert is_error(refused) and "confirm=true" in text(refused)
