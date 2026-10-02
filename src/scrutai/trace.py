"""Tracing: see exactly what the panel did, call by call.

Three sinks, all optional:

* JSONL (`scrutai review --trace run.jsonl`): start/end events for every graph
  node, LLM call and tool call (timings, token deltas), plus the run plan, each
  finding raised, each critic decision and each defense. Zero dependencies.
* Listeners: in-process callbacks; `scrutai serve` streams them to the browser.
* OpenTelemetry (`tracing: otel`): a span per node / LLM call / tool call, sent
  wherever your OTel SDK is configured to export. Needs `opentelemetry-api`.
* Langfuse (`tracing: langfuse`): LiteLLM's built-in callback logs every live
  model call. Needs the `langfuse` package and LANGFUSE_* keys.

The active tracer is process-global for the duration of one review (graph
nodes run on worker threads, so a context variable would not reach them).
"""

from __future__ import annotations

import contextlib
import functools
import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from .llm import LLMClient

Listener = Callable[[dict[str, Any]], None]


class Tracer:
    """Fans events out to the configured sinks.

    Every event carries the review's `run` id and a monotonically increasing
    `seq`. Spans emit a `phase: "start"` event when work begins and a
    `phase: "end"` event (with `ms`, results, and any `error`) when it ends, so
    a live viewer can animate work in progress. `listeners` receive every event
    as it happens (the web UI's event bus is one).
    """

    def __init__(
        self,
        jsonl: str | None = None,
        otel: bool = False,
        listeners: list[Listener] | None = None,
        run_id: str | None = None,
    ) -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._lock = threading.Lock()
        self._seq = 0
        self._listeners = list(listeners or [])
        # Held open for the whole review and closed by close(); events stream in.
        self._fh = Path(jsonl).open("a", encoding="utf-8") if jsonl else None  # noqa: SIM115
        self._otel: Any = None
        if otel:
            try:
                from opentelemetry import trace as ot

                self._otel = ot.get_tracer("scrutai")
            except ImportError as exc:
                raise RuntimeError(
                    "tracing: otel needs opentelemetry-api (pip install scrutai[otel])"
                ) from exc

    def event(self, kind: str, **fields: Any) -> None:
        if self._fh is None and not self._listeners:
            return
        with self._lock:
            self._seq += 1
            record = {
                "run": self.run_id,
                "seq": self._seq,
                "ts": round(time.time(), 3),
                "kind": kind,
                **fields,
            }
            if self._fh is not None:
                self._fh.write(json.dumps(record, default=str) + "\n")
                self._fh.flush()
            listeners = list(self._listeners)
        for listener in listeners:
            # A broken viewer must never break a review.
            with contextlib.suppress(Exception):
                listener(record)

    @contextlib.contextmanager
    def span(self, kind: str, name: str, **attrs: Any) -> Iterator[dict[str, Any]]:
        """Time a unit of work; callers may add result fields to the yielded dict."""
        extra: dict[str, Any] = {}
        span_id = uuid.uuid4().hex[:8]
        self.event(kind, name=name, id=span_id, phase="start", **attrs)
        start = time.perf_counter()
        otel_cm = (
            self._otel.start_as_current_span(f"scrutai.{kind}.{name}")
            if self._otel is not None
            else contextlib.nullcontext()
        )
        error: str | None = None
        with otel_cm as span:
            try:
                yield extra
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                ms = round((time.perf_counter() - start) * 1000, 1)
                fields = {**attrs, **extra, "ms": ms, **({"error": error} if error else {})}
                if span is not None:
                    for k, v in fields.items():
                        span.set_attribute(
                            f"scrutai.{k}", v if isinstance(v, int | float | str | bool) else str(v)
                        )
                self.event(kind, name=name, id=span_id, phase="end", **fields)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


_active: Tracer | None = None


def active() -> Tracer | None:
    return _active


@contextlib.contextmanager
def tracing(tracer: Tracer | None) -> Iterator[None]:
    global _active
    previous, _active = _active, tracer
    try:
        yield
    finally:
        _active = previous


@contextlib.contextmanager
def span(kind: str, name: str, **attrs: Any) -> Iterator[dict[str, Any]]:
    """Module-level helper: a no-op unless a tracer is active."""
    tracer = _active
    if tracer is None:
        yield {}
        return
    with tracer.span(kind, name, **attrs) as extra:
        yield extra


def emit(kind: str, **fields: Any) -> None:
    """Module-level helper: record a point event if a tracer is active."""
    tracer = _active
    if tracer is not None:
        tracer.event(kind, **fields)


def finding_ref(f: Any) -> dict[str, Any]:
    """The fields a viewer needs to follow one finding through the trial."""
    return {
        "finding": f"{f.file}:{f.line}:{f.category}",
        "agent": f.agent,
        "category": f.category,
        "title": f.title,
        "file": f.file,
        "line": f.line,
        "severity": f.severity.value,
        "confidence": round(f.confidence, 3),
    }


def traced_node[**P, R](name: str, fn: Callable[P, R]) -> Callable[P, R]:
    """Wrap a graph node in a span; ParamSpec keeps the signature LangGraph checks."""

    @functools.wraps(fn)
    def run(*args: P.args, **kwargs: P.kwargs) -> R:
        with span("node", name):
            return fn(*args, **kwargs)

    return run


class TracingClient:
    """Wraps an LLM client and records every call."""

    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner

    @property
    def tokens_used(self) -> int:
        return self.inner.tokens_used

    @property
    def cost_usd(self) -> float:
        return self.inner.cost_usd

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        role = prompt.split("\n", 1)[0].removeprefix("ROLE:").strip() or "unknown"
        if "\nMODE: defend" in prompt:
            role += ":defend"
        before = self.inner.tokens_used
        with span("llm", role, model=model, prompt_chars=len(prompt)) as extra:
            reply = self.inner.complete(model=model, system=system, prompt=prompt)
            # Calls run concurrently, so a before/after delta of the shared
            # counter would include other threads' calls; prefer the client's
            # per-thread figure and fall back to the delta for custom clients.
            exact = getattr(self.inner, "last_call_tokens", None)
            extra["tokens"] = exact if exact is not None else self.inner.tokens_used - before
            extra["reply_chars"] = len(reply)
        return reply


def enable_langfuse() -> None:
    """Route LiteLLM's success/failure callbacks to Langfuse (live mode only)."""
    import litellm

    for hook in (litellm.success_callback, litellm.failure_callback):
        if "langfuse" not in hook:
            hook.append("langfuse")
