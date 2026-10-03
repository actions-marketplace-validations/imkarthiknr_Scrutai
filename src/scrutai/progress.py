"""Turn a review's trace events into "step N of M" progress.

Front ends that can show progress (the MCP server's progress notifications, a
future CLI spinner) subscribe a `ProgressListener` to the review's tracer.
Progress is monotonic and reaches `total` exactly once, when the verdict is in.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

ProgressCallback = Callable[[float, float, str], None]


class ProgressListener:
    """A tracer listener that reports (done, total, message).

    The total is known once routing has planned the work: one step per
    (specialist, chunk) task, one for the critic and one for the verdict.
    """

    def __init__(self, on_progress: ProgressCallback) -> None:
        self.on_progress = on_progress
        self.done = 0
        self.total = 2  # critic + verdict, before the plan is known
        self._critic_counted = False
        self._lock = threading.Lock()

    def __call__(self, event: dict[str, Any]) -> None:
        kind, name, phase = event.get("kind"), event.get("name"), event.get("phase")
        message: str | None = None
        with self._lock:
            if kind == "plan":
                tasks = event.get("tasks") or []
                self.total = len(tasks) + 2
                agents = ", ".join(event.get("agents") or []) or "nobody (nothing to review)"
                message = f"Routed {len(tasks)} task(s) to {agents}"
            elif kind == "node" and name == "specialist" and phase == "end":
                self.done += 1
                message = (
                    f"{event.get('agent')} finished chunk {event.get('chunk')} "
                    f"({event.get('findings', 0)} raised)"
                )
            elif kind == "node" and name == "critic" and phase == "start":
                message = "Critic cross-examining findings"
            elif (
                kind == "node" and name == "critic" and phase == "end" and not self._critic_counted
            ):
                self._critic_counted = True
                self.done += 1
                message = "Critic finished its first round"
            elif kind == "node" and name == "defend" and phase == "start":
                message = "Specialists defending challenged findings"
            elif kind == "node" and name == "verdict" and phase == "end":
                self.done = self.total
                message = "Verdict reached"
            if message is None:
                return
            done, total = min(self.done, self.total), self.total
        self.on_progress(float(done), float(total), message)
