"""Provider-agnostic LLM access.

The rest of the codebase depends only on the `LLMClient` protocol, never on a
vendor SDK. That keeps the system model-agnostic (a requirement for the eval
harness, which compares models) and lets every test run offline against
`MockLLMClient`.
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    def complete(self, *, model: str, system: str, prompt: str) -> str: ...

    @property
    def tokens_used(self) -> int: ...


class MockLLMClient:
    """Deterministic offline client.

    Returns canned findings keyed off substrings in the prompt so the full
    pipeline runs end-to-end with no API key. Swap for `LiteLLMClient` to go
    live. This is also what the deterministic tests run against.
    """

    def __init__(self) -> None:
        self._tokens = 0

    @property
    def tokens_used(self) -> int:
        return self._tokens

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        self._tokens += len(prompt) // 4  # rough token accounting for the demo
        text = prompt.lower()

        # --- critic role: judge a finding, return an updated confidence ---
        if "you are the critic" in system.lower():
            # Kill findings with no concrete evidence; keep the rest.
            weak = "evidence: none" in text or "no evidence" in text
            return json.dumps(
                {
                    "confidence": 0.2 if weak else 0.82,
                    "note": (
                        "No concrete line or tool result cited; downgraded."
                        if weak
                        else "Backed by a cited line and tool output; upheld."
                    ),
                }
            )

        # --- specialist role: propose findings for the diff ---
        findings: list[dict] = []
        if "security" in system.lower():
            if "eval(" in text or "subprocess" in text or "os.system" in text:
                findings.append(
                    {
                        "title": "Possible command/eval injection",
                        "body": "Untrusted input reaches a dynamic execution sink.",
                        "severity": "high",
                        "confidence": 0.7,
                        "evidence": ["grep matched a dynamic-execution call in the diff"],
                    }
                )
        if "correctness" in system.lower():
            if "except:" in text or "except exception" in text:
                findings.append(
                    {
                        "title": "Overly broad exception handler",
                        "body": "Bare/broad except swallows errors and hides bugs.",
                        "severity": "medium",
                        "confidence": 0.65,
                        "evidence": ["diff adds a broad except clause"],
                    }
                )
        if "test" in system.lower():
            if "def " in text and "test_" not in text:
                findings.append(
                    {
                        "title": "New logic added without tests",
                        "body": "Changed function has no accompanying test in the diff.",
                        "severity": "low",
                        "confidence": 0.55,
                        "evidence": ["no test_* additions found for the changed symbol"],
                    }
                )
        return json.dumps({"findings": findings})


class LiteLLMClient:
    """Live client. Requires `litellm` and provider credentials in the env."""

    def __init__(self) -> None:
        self._tokens = 0

    @property
    def tokens_used(self) -> int:
        return self._tokens

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        import litellm  # imported lazily so mock runs need no dependency

        resp = litellm.completion(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self._tokens += getattr(usage, "total_tokens", 0)
        return resp.choices[0].message.content or ""


def make_client(mode: str) -> LLMClient:
    return LiteLLMClient() if mode == "live" else MockLLMClient()
