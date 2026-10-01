"""Provider-agnostic LLM access.

The rest of the codebase depends only on the `LLMClient` protocol, never on a
vendor SDK. That keeps the system model-agnostic (a requirement for the eval
harness, which compares models) and lets every test run offline against
`MockLLMClient`.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
from typing import Any, Protocol, runtime_checkable


class LLMError(RuntimeError):
    """A provider call failed (network, auth, rate limit, empty response)."""


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(raw: str) -> dict[str, Any]:
    """Parse the JSON object in a model reply.

    Real models wrap JSON in ```json fences or add a sentence before it; accept
    both. Raises json.JSONDecodeError if no object can be recovered.
    """
    text = raw.strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise json.JSONDecodeError("expected a JSON object", text, 0)
    return value


@runtime_checkable
class LLMClient(Protocol):
    def complete(self, *, model: str, system: str, prompt: str) -> str: ...

    @property
    def tokens_used(self) -> int: ...

    @property
    def cost_usd(self) -> float: ...


class LiteLLMClient:
    """Live client. Requires `litellm` and provider credentials in the env."""

    def __init__(self) -> None:
        self._tokens = 0
        self._cost = 0.0
        self._lock = threading.Lock()

    @property
    def tokens_used(self) -> int:
        return self._tokens

    @property
    def cost_usd(self) -> float:
        return self._cost

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        import litellm  # imported lazily so mock runs need no provider setup

        try:
            resp = litellm.completion(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                # No temperature: current Claude models reject non-default
                # sampling params; determinism comes from the critic, not sampling.
                num_retries=2,
            )
        except Exception as exc:  # provider SDKs raise many unrelated types
            raise LLMError(f"{model}: {exc}") from exc
        usage = getattr(resp, "usage", None)
        tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage is not None else 0
        cost = 0.0
        # Unknown model pricing raises; tokens are still tracked.
        with contextlib.suppress(Exception):
            cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
        with self._lock:  # calls arrive from several threads
            self._tokens += tokens
            self._cost += cost
        content = resp.choices[0].message.content
        if not content:
            raise LLMError(f"{model}: empty response")
        return str(content)


class BudgetExceeded(LLMError):
    """The review hit its token or dollar budget; no further calls are made."""


class BudgetedClient:
    """Wraps any client and refuses calls once a budget is spent.

    The check happens before each call, so concurrent calls can overshoot by
    at most one call each: a soft cap that never runs away.
    """

    def __init__(self, inner: LLMClient, token_budget: int, max_cost_usd: float = 0.0) -> None:
        self.inner = inner
        self.token_budget = token_budget
        self.max_cost_usd = max_cost_usd
        self.exhausted = False
        self._lock = threading.Lock()

    @property
    def tokens_used(self) -> int:
        return self.inner.tokens_used

    @property
    def cost_usd(self) -> float:
        return self.inner.cost_usd

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        with self._lock:
            over_tokens = self.token_budget > 0 and self.inner.tokens_used >= self.token_budget
            over_cost = self.max_cost_usd > 0 and self.inner.cost_usd >= self.max_cost_usd
            if over_tokens or over_cost:
                self.exhausted = True
            if self.exhausted:
                raise BudgetExceeded(
                    f"budget spent: {self.inner.tokens_used} tokens, ${self.inner.cost_usd:.4f}"
                )
        return self.inner.complete(model=model, system=system, prompt=prompt)


def make_client(mode: str) -> LLMClient:
    if mode == "live":
        return LiteLLMClient()
    if mode == "mock":
        return MockLLMClient()
    raise ValueError(f"unknown llm_mode {mode!r} (expected 'mock' or 'live')")


# Re-exported so `from scrutai.llm import MockLLMClient` keeps working.
from .mock import MockLLMClient  # noqa: E402

__all__ = [
    "BudgetExceeded",
    "BudgetedClient",
    "LLMClient",
    "LLMError",
    "LiteLLMClient",
    "MockLLMClient",
    "extract_json",
    "make_client",
]
