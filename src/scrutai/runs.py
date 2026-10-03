"""Reviews in flight and finished: shared by the web server and the MCP server.

A run is a review's event stream plus its status. Front ends append trace
events as the review progresses and read them back (the web UI streams them;
MCP turns them into progress notifications and resources).
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Literal

MAX_EVENTS_PER_RUN = 50_000
MAX_RUNS = 50


class RunNotFound(KeyError):
    """No run with this id (never existed, or evicted)."""


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
    # What a front end keeps besides the events (the MCP server: the full result).
    outcome: Any = None
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
            raise RunNotFound(run_id)
        return run

    def all(self) -> list[Run]:
        with self._lock:
            return list(reversed(self._runs.values()))


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
