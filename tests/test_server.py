"""The agent theater backend: runs, the SSE stream, replay, and input validation."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from scrutai.web.server import create_app  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _wait(client: TestClient, run_id: str, timeout: float = 20) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run: dict[str, Any] = client.get(f"/api/runs/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def _sse(client: TestClient, run_id: str, last_id: str | None = None) -> list[dict[str, Any]]:
    headers = {"Last-Event-ID": last_id} if last_id else {}
    events: list[dict[str, Any]] = []
    with client.stream("GET", f"/api/runs/{run_id}/events", headers=headers) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def test_demo_run_streams_the_whole_review(client: TestClient) -> None:
    started = client.post("/api/runs", json={"source": "demo"})
    assert started.status_code == 202
    run_id = started.json()["id"]
    events = _sse(client, run_id)  # follows the live run to its end
    kinds = [e.get("kind") for e in events]
    assert kinds[0] == "review" and "plan" in kinds and "decision" in kinds
    assert events[-1] == {"status": "done"}  # the end sentinel
    run = _wait(client, run_id)
    assert run["verdict"] == "request_changes"
    assert {f["category"] for f in run["result"]["findings"]} >= {"injection", "broad_except"}
    assert [r["id"] for r in client.get("/api/runs").json()] == [run_id]


def test_resume_with_last_event_id(client: TestClient) -> None:
    run_id = client.post("/api/runs", json={"source": "demo"}).json()["id"]
    _wait(client, run_id)
    full = [e for e in _sse(client, run_id) if "seq" in e]
    tail = [e for e in _sse(client, run_id, last_id="10") if "seq" in e]
    assert tail == full[10:]


def test_git_refs_cannot_inject_options(
    client: TestClient, git_repo: Callable[[dict[str, str]], Path]
) -> None:
    app = create_app(repo=str(git_repo({"a.py": "x = 1\n"})))
    c = TestClient(app)
    run_id = c.post("/api/runs", json={"source": "git", "base": "--output=/tmp/pwned"}).json()["id"]
    run = _wait(c, run_id)
    assert run["status"] == "error" and "invalid git ref" in run["error"]
    assert not Path("/tmp/pwned").exists()


def test_git_range_run(git_repo: Callable[[dict[str, str]], Path]) -> None:
    repo = git_repo({"svc.py": "import pickle\n\n\ndef _l(b):\n    return pickle.loads(b)\n"})
    c = TestClient(create_app(repo=str(repo)))
    run = _wait(c, c.post("/api/runs", json={"source": "git", "base": "main"}).json()["id"])
    assert run["status"] == "done"
    assert "unsafe_deserialization" in {f["category"] for f in run["result"]["findings"]}


def test_patch_run_and_validation(client: TestClient) -> None:
    assert client.post("/api/runs", json={"source": "patch"}).status_code == 422
    assert client.post("/api/runs", json={"source": "pr"}).status_code == 422
    patch = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+eval(data)\n"
    run = _wait(
        client, client.post("/api/runs", json={"source": "patch", "patch": patch}).json()["id"]
    )
    assert run["result"]["findings"][0]["category"] == "injection"


def test_replay_a_trace_file(client: TestClient, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from scrutai.cli import app

    trace = tmp_path / "t.jsonl"
    CliRunner().invoke(app, ["review", "--demo", "--trace", str(trace)])
    resp = client.post("/api/replay", content=trace.read_bytes())
    assert resp.status_code == 201
    run = resp.json()
    assert run["status"] == "done" and run["verdict"] == "request_changes"
    replayed = [e for e in _sse(client, run["id"]) if "seq" in e]
    assert replayed == [json.loads(line) for line in trace.read_text().splitlines()]
    assert client.post("/api/replay", content=b"not a trace").status_code == 422


def test_unknown_run_is_404(client: TestClient) -> None:
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope/events").status_code == 404


def test_health(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["ok"] and body["llm_mode"] == "mock"
