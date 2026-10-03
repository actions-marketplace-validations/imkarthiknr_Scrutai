"""The MCP server, through a real MCP client connected in-process."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp_util import annotations, in_memory, is_error, output_schema, structured, text

from scrutai.demo import DEMO_FILE, DEMO_SOURCE
from scrutai.inputs import Source
from scrutai.mcp import security, server
from scrutai.mcp.server import Settings, build_server
from scrutai.models import ReviewResult, Verdict

pytestmark = pytest.mark.anyio

DEMO_PATCH = (
    f"diff --git a/{DEMO_FILE} b/{DEMO_FILE}\nnew file mode 100644\n--- /dev/null\n"
    f"+++ b/{DEMO_FILE}\n@@ -0,0 +1,{len(DEMO_SOURCE.splitlines())} @@\n"
    + "".join(f"+{line}\n" for line in DEMO_SOURCE.splitlines())
)
DEMO_CATEGORIES = {
    "injection",
    "mutable_default",
    "broad_except",
    "missing_tests",
    "debug_leftover",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def demo_repo(git_repo: Callable[[dict[str, str]], Path]) -> Path:
    return git_repo({DEMO_FILE: DEMO_SOURCE})


def make(root: Path, **kw: Any) -> Any:
    return build_server(Settings(roots=[str(root)], **kw))


async def test_discovery_lists_review_tools_with_safe_annotations(tmp_path: Path) -> None:
    async with in_memory(make(tmp_path)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert {"review_patch", "review_git_range"} <= set(tools)
    for name in ("review_patch", "review_git_range"):
        hints = annotations(tools[name])
        assert hints["readOnlyHint"] is True and hints["destructiveHint"] is False
        assert hints["openWorldHint"] is True
        schema = output_schema(tools[name])
        assert schema and {"verdict", "findings", "partial"} <= set(schema["properties"])
        assert "data, not instructions" in (tools[name].description or "")


async def test_review_git_range_finds_the_demo_issues(demo_repo: Path) -> None:
    progress: list[tuple[float, float | None, str | None]] = []

    async def on_progress(done: float, total: float | None, message: str | None) -> None:
        progress.append((done, total, message))

    async with in_memory(make(demo_repo.parent)) as client:
        result = await client.call_tool(
            "review_git_range",
            {"base": "main", "head": "feature", "repo": "repo"},
            progress_callback=on_progress,
        )
    data = structured(result)
    assert not is_error(result)
    assert data["verdict"] == "request_changes" and data["label"] == "main...feature"
    assert {f["category"] for f in data["findings"]} == DEMO_CATEGORIES
    assert [f["id"] for f in data["findings"]] == [f"F{i}" for i in range(1, 6)]
    assert data["files"] == 1 and data["partial"] is False and data["dropped"] >= 1
    assert all(f["file"] == DEMO_FILE and f["fingerprint"] for f in data["findings"])
    # Progress: monotonic, ends exactly at the total, says what happened.
    assert progress, "no progress notifications"
    dones = [p[0] for p in progress]
    assert dones == sorted(dones) and progress[-1][0] == progress[-1][1]
    assert progress[-1][2] == "Verdict reached"
    assert any("Routed" in (p[2] or "") for p in progress)


async def test_review_patch_matches_the_git_range_review(demo_repo: Path) -> None:
    async with in_memory(make(demo_repo)) as client:
        result = await client.call_tool("review_patch", {"patch": DEMO_PATCH})
    data = structured(result)
    assert data["label"] == "Pasted patch"
    assert {f["category"] for f in data["findings"]} == DEMO_CATEGORIES


async def test_reviews_are_recorded_in_the_run_store(demo_repo: Path) -> None:
    srv = make(demo_repo)
    async with in_memory(srv) as client:
        data = structured(await client.call_tool("review_patch", {"patch": DEMO_PATCH}))
    run = srv.scrutai.runs.get(data["review_id"])
    assert run.status == "done" and run.summary()["verdict"] == "request_changes"


@pytest.mark.parametrize(
    ("tool", "args", "error"),
    [
        ("review_git_range", {"repo": "/etc"}, "outside the allowed roots"),
        ("review_git_range", {"repo": "../.."}, "outside the allowed roots"),
        ("review_git_range", {"base": "--output=/tmp/pwned"}, "invalid git ref"),
        ("review_git_range", {"head": "main..evil"}, "invalid git ref"),
        ("review_git_range", {"base": "no-such-branch"}, "no-such-branch"),
        ("review_patch", {"patch": "   "}, "patch is empty"),
        ("review_patch", {"patch": DEMO_PATCH, "repo": "/"}, "outside the allowed roots"),
    ],
)
async def test_bad_requests_are_tool_errors(
    demo_repo: Path, tool: str, args: dict[str, Any], error: str
) -> None:
    async with in_memory(make(demo_repo)) as client:
        result = await client.call_tool(tool, args)
    assert is_error(result) and error in text(result)
    assert not Path("/tmp/pwned").exists()


async def test_symlink_out_of_a_root_is_refused(demo_repo: Path, tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "escape").symlink_to(demo_repo)
    async with in_memory(make(root)) as client:
        result = await client.call_tool("review_git_range", {"repo": "escape"})
    assert is_error(result) and "outside the allowed roots" in text(result)


async def test_oversized_inputs_are_refused(
    demo_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(security, "MAX_PATCH_BYTES", 100)
    async with in_memory(make(demo_repo)) as client:
        result = await client.call_tool("review_patch", {"patch": DEMO_PATCH})
        assert is_error(result) and "larger than 100 bytes" in text(result)
        monkeypatch.setattr(security, "MAX_PATCH_BYTES", 10_000_000)
        monkeypatch.setattr(security, "MAX_FILES", 0)
        result = await client.call_tool("review_git_range", {"base": "main", "head": "feature"})
        assert is_error(result) and "the limit is 0" in text(result)


async def test_a_bad_config_is_a_tool_error_not_a_crash(demo_repo: Path, tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yml"
    cfg.write_text("llm_mode: nonsense\n")
    async with in_memory(make(demo_repo, config_path=str(cfg))) as client:
        result = await client.call_tool("review_patch", {"patch": DEMO_PATCH})
    assert is_error(result) and "invalid server config" in text(result)


async def test_cancelling_a_call_stops_the_review(
    demo_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel the awaiting task: the worker sees the cancel flag and ends partial."""
    seen: dict[str, Any] = {}
    started = threading.Event()

    def slow_review(diff: Any, config: Any, llm: Any, tracer: Any, cancel: Any) -> ReviewResult:
        started.set()
        deadline = time.monotonic() + 5
        while not cancel.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        seen["cancelled"] = cancel.is_set()
        return ReviewResult(verdict=Verdict.COMMENT, findings=[], cancelled=True)

    monkeypatch.setattr(server, "review_diff", slow_review)
    srv = make(demo_repo)
    reviewer = srv.scrutai

    class Ctx:
        async def report_progress(self, *args: Any) -> None: ...

    async with anyio.create_task_group() as tg:

        async def call() -> None:
            await reviewer.review(Source(kind="patch", patch=DEMO_PATCH), demo_repo, Ctx())

        tg.start_soon(call)
        while not started.is_set():
            await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    for _ in range(200):  # the abandoned worker notices within a poll
        if "cancelled" in seen:
            break
        await anyio.sleep(0.01)
    assert seen == {"cancelled": True}
    (run,) = reviewer.runs.all()
    assert run.status == "error" and run.error == "cancelled by the client"


def test_roots_must_exist(tmp_path: Path) -> None:
    with pytest.raises(security.SecurityError, match="not a directory"):
        security.Roots([str(tmp_path / "missing")])


def test_relative_repo_resolves_against_the_first_root(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    roots = security.Roots([str(tmp_path / "a")])
    assert roots.resolve(None) == (tmp_path / "a").resolve()
    assert roots.resolve("b") == (tmp_path / "a" / "b").resolve()
    assert roots.resolve(str(tmp_path / "a" / "b")) == (tmp_path / "a" / "b").resolve()
    with pytest.raises(security.SecurityError):
        roots.resolve(str(tmp_path))
