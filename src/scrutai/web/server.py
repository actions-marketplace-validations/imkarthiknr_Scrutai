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
import threading
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import ScrutaiConfig
from ..diff import DiffError
from ..github import GitHubError
from ..inputs import Source, prepare
from ..llm import make_client
from ..orchestrator import review_diff
from ..runs import Run, RunNotFound, RunStore, load_trace
from ..trace import Tracer

STATIC_DIR = Path(__file__).resolve().parent / "static"


class RunRequest(BaseModel):
    source: Literal["demo", "git", "patch", "pr"] = "demo"
    base: str = "main"
    head: str = "HEAD"
    patch: str | None = Field(default=None, max_length=5_000_000)
    pr: int | None = None


def _label(req: RunRequest) -> str:
    return Source(kind=req.source, base=req.base, head=req.head, pr=int(req.pr or 0)).label()


def create_app(
    config_path: str = ".scrutai.yml", repo: str = ".", store: RunStore | None = None
) -> FastAPI:
    app = FastAPI(title="Scrutai", docs_url="/api/docs", openapi_url="/api/openapi.json")
    runs = store or RunStore()
    app.state.runs = runs

    def execute(run: Run, req: RunRequest) -> None:
        tracer = Tracer(listeners=[run.append], run_id=run.id)
        source = Source(
            kind=req.source,
            base=req.base,
            head=req.head,
            patch=req.patch or "",
            pr=int(req.pr or 0),
        )
        try:
            config = ScrutaiConfig.load(config_path)
            with prepare(source, config, repo) as prepared:
                review_diff(prepared.diff, config, make_client(config.llm_mode), tracer)
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

    def _get(run_id: str) -> Run:
        try:
            return runs.get(run_id)
        except RunNotFound as exc:
            raise HTTPException(404, f"no run {run_id!r}") from exc

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
        return _get(run_id).summary()

    @app.get("/api/runs/{run_id}/events")
    async def stream(run_id: str, request: Request) -> StreamingResponse:
        run = _get(run_id)
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
