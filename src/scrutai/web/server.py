"""`scrutai serve`: the agent theater backend.

Runs reviews in the background and streams their trace events to the browser
over Server-Sent Events. A run is a list of events plus a status; the UI is
driven entirely by events, so a live run and a replayed `--trace` file look the
same to it.

    POST /api/runs               start a review (demo | git range | patch | PR)
    GET  /api/runs               recent runs
    GET  /api/runs/{id}          one run: status, event count, final result
    GET  /api/runs/{id}/events   SSE stream (replays history, then live;
                                 honours Last-Event-ID to resume)
    POST /api/replay             load a JSONL trace as a finished run

Binds to 127.0.0.1 by default: it can read your repository and spend your API
budget, so do not expose it to a network you don't trust.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import ScrutaiConfig
from ..demo import review_demo
from ..diff import DiffError, apply_filters, diff_from_git, parse_diff
from ..github import GitHubClient, GitHubError, detect_repo
from ..llm import make_client
from ..orchestrator import review_diff
from ..trace import Tracer

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_EVENTS_PER_RUN = 50_000
MAX_RUNS = 50


@dataclass
class Run:
    id: str
    source: str
    label: str
    created: float = field(default_factory=time.time)
    status: Literal["running", "done", "error"] = "running"
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, event: dict[str, Any]) -> None:
        with self.lock:
            if len(self.events) >= MAX_EVENTS_PER_RUN:
                self.truncated = True
                return
            self.events.append(event)

    def since(self, index: int) -> list[dict[str, Any]]:
        with self.lock:
            return self.events[index:]

    def summary(self) -> dict[str, Any]:
        result = next((e["result"] for e in reversed(self.events) if e["kind"] == "result"), None)
        return {
            "id": self.id,
            "source": self.source,
            "label": self.label,
            "created": self.created,
            "status": self.status,
            "error": self.error,
            "events": len(self.events),
            "truncated": self.truncated,
            "verdict": result["verdict"] if result else None,
            "result": result,
        }


class RunStore:
    """In-memory runs, oldest finished ones evicted beyond MAX_RUNS."""

    def __init__(self) -> None:
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, run: Run) -> Run:
        with self._lock:
            self._runs[run.id] = run
            while len(self._runs) > MAX_RUNS:
                oldest = next((k for k, r in self._runs.items() if r.status != "running"), None)
                if oldest is None:
                    break
                del self._runs[oldest]
        return run

    def get(self, run_id: str) -> Run:
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            raise HTTPException(404, f"no run {run_id!r}")
        return run

    def all(self) -> list[Run]:
        with self._lock:
            return list(reversed(self._runs.values()))


class RunRequest(BaseModel):
    source: Literal["demo", "git", "patch", "pr"] = "demo"
    base: str = "main"
    head: str = "HEAD"
    patch: str | None = Field(default=None, max_length=5_000_000)
    pr: int | None = None


def _label(req: RunRequest) -> str:
    return {
        "demo": "Demo: app/runner.py",
        "git": f"{req.base}...{req.head}",
        "patch": "Pasted patch",
        "pr": f"PR #{req.pr}",
    }[req.source]


def load_trace(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {n}: not JSON") from exc
        if not isinstance(event, dict) or "kind" not in event:
            raise ValueError(f"line {n}: not a Scrutai trace event")
        events.append(event)
    if not events:
        raise ValueError("empty trace")
    return events


def create_app(
    config_path: str = ".scrutai.yml", repo: str = ".", store: RunStore | None = None
) -> FastAPI:
    app = FastAPI(title="Scrutai", docs_url="/api/docs", openapi_url="/api/openapi.json")
    runs = store or RunStore()
    app.state.runs = runs

    def execute(run: Run, req: RunRequest) -> None:
        tracer = Tracer(listeners=[run.append], run_id=run.id)
        try:
            config = ScrutaiConfig.load(config_path)
            if req.source == "demo":
                review_demo(config, tracer)
                return
            if req.source == "git":
                diff = diff_from_git(req.base, req.head, repo)
            elif req.source == "patch":
                diff = parse_diff(req.patch or "", repo_root=repo)
            else:
                client = GitHubClient(
                    os.environ.get("GITHUB_TOKEN", ""),
                    detect_repo(repo),
                    os.environ.get("GITHUB_API_URL", "https://api.github.com"),
                )
                diff = parse_diff(client.pull_request_diff(int(req.pr or 0)), repo_root=repo)
            diff = apply_filters(diff, config.include, config.exclude)
            review_diff(diff, config, make_client(config.llm_mode), tracer)
        except (DiffError, GitHubError, ValueError, OSError) as exc:
            run.error = str(exc)
            run.status = "error"
            tracer.event("error", message=str(exc))
        except Exception as exc:  # surface anything else to the UI, don't kill the server
            run.error = f"{type(exc).__name__}: {exc}"
            run.status = "error"
            tracer.event("error", message=run.error)
        finally:
            if run.status == "running":
                run.status = "done"

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        cfg = ScrutaiConfig.load(config_path)
        return {"ok": True, "llm_mode": cfg.llm_mode, "agents": cfg.enabled_agents}

    @app.get("/api/runs")
    def list_runs() -> list[dict[str, Any]]:
        return [{k: v for k, v in r.summary().items() if k != "result"} for r in runs.all()]

    @app.post("/api/runs", status_code=202)
    def start_run(req: RunRequest) -> dict[str, Any]:
        if req.source == "pr" and not req.pr:
            raise HTTPException(422, "source 'pr' needs a PR number")
        if req.source == "patch" and not (req.patch or "").strip():
            raise HTTPException(422, "source 'patch' needs a unified diff")
        run = runs.add(Run(id=uuid.uuid4().hex[:12], source=req.source, label=_label(req)))
        threading.Thread(target=execute, args=(run, req), daemon=True).start()
        return run.summary()

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        return runs.get(run_id).summary()

    @app.get("/api/runs/{run_id}/events")
    async def stream(run_id: str, request: Request) -> StreamingResponse:
        run = runs.get(run_id)
        last = request.headers.get("last-event-id", "")
        start = int(last) if last.isdigit() else 0

        async def events() -> AsyncIterator[str]:
            sent = start
            idle = 0.0
            while True:
                batch = run.since(sent)
                for event in batch:
                    yield f"id: {sent + 1}\ndata: {json.dumps(event, default=str)}\n\n"
                    sent += 1
                if not batch:
                    if run.status != "running":
                        yield f"event: end\ndata: {json.dumps({'status': run.status})}\n\n"
                        return
                    if await request.is_disconnected():
                        return
                    idle += 0.05
                    if idle >= 15:  # keep proxies from closing an idle stream
                        idle = 0.0
                        yield ": keep-alive\n\n"
                    await asyncio.sleep(0.05)
                else:
                    idle = 0.0

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/replay", status_code=201)
    async def replay(request: Request) -> dict[str, Any]:
        body = (await request.body()).decode("utf-8", errors="replace")
        try:
            events = load_trace(body)
        except ValueError as exc:
            raise HTTPException(422, f"invalid trace: {exc}") from exc
        run = Run(id=uuid.uuid4().hex[:12], source="replay", label="Replayed trace")
        for e in events:
            run.append(e)
        run.status = "done"
        runs.add(run)
        return run.summary()

    if (STATIC_DIR / "index.html").is_file():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    else:

        @app.get("/", response_class=HTMLResponse)
        def no_ui() -> str:
            return (
                "<h1>Scrutai</h1><p>The UI bundle is not built. Run <code>npm ci && npm run "
                "build</code> in <code>web/</code>, or use the API at "
                "<a href='/api/docs'>/api/docs</a>.</p>"
            )

    return app
