"""Tracing: see exactly what the panel did, call by call.

Three sinks, all optional:

* JSONL (`scrutai review --trace run.jsonl`): one event per graph node, LLM
  call and tool call, with timings and token deltas. Zero dependencies.
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
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from .llm import LLMClient


class Tracer:
    """Fans events out to the configured sinks."""

    def __init__(self, jsonl: str | None = None, otel: bool = False) -> None:
        self._lock = threading.Lock()
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
        if self._fh is None:
            return
        record = {"ts": round(time.time(), 3), "kind": kind, **fields}
        line = json.dumps(record, default=str)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    @contextlib.contextmanager
    def span(self, kind: str, name: str, **attrs: Any) -> Iterator[dict[str, Any]]:
        """Time a unit of work; callers may add result fields to the yielded dict."""
        extra: dict[str, Any] = {}
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
                self.event(kind, name=name, **fields)

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
            extra["tokens"] = self.inner.tokens_used - before
            extra["reply_chars"] = len(reply)
        return reply


def enable_langfuse() -> None:
    """Route LiteLLM's success/failure callbacks to Langfuse (live mode only)."""
    import litellm

    for hook in (litellm.success_callback, litellm.failure_callback):
        if "langfuse" not in hook:
            hook.append("langfuse")
