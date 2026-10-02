"""The live path through real LiteLLM objects, with the network call replaced.

LiteLLM's own `mock_response` builds genuine ModelResponse objects (usage and
cost included); Scrutai's mock brain decides what the reply says. Everything
between Scrutai and the provider wire therefore runs for real.
"""

from __future__ import annotations

from typing import Any

import pytest

from scrutai import review_diff
from scrutai.config import ScrutaiConfig
from scrutai.llm import LiteLLMClient, LLMError, make_client
from scrutai.mock import MockLLMClient
from scrutai.models import ChangedFile, DiffContext

litellm = pytest.importorskip("litellm")


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Route litellm.completion to its mock_response mode, answered by the mock brain."""
    brain = MockLLMClient()
    real = litellm.completion
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> Any:
        calls.append(kwargs)
        system, user = kwargs["messages"][0]["content"], kwargs["messages"][1]["content"]
        reply = brain.complete(model=kwargs["model"], system=system, prompt=user)
        return real(**kwargs, mock_response=reply)

    monkeypatch.setattr(litellm, "completion", fake)
    return calls


def test_full_review_in_live_mode(wire: list[dict[str, Any]]) -> None:
    diff = DiffContext(
        files=[ChangedFile(path="lib/a.py", patch="+def _f(cmd):\n+    os.system(cmd)\n")]
    )
    cfg = ScrutaiConfig(llm_mode="live")
    llm = make_client(cfg.llm_mode)
    assert isinstance(llm, LiteLLMClient)
    result = review_diff(diff, cfg, llm)
    assert [f.category for f in result.findings] == ["injection"]
    assert result.tokens_used > 0 and result.cost_usd > 0  # real usage + pricing from LiteLLM
    models = {c["model"] for c in wire}
    assert {"anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5"} <= models
    # Current Claude models reject non-default sampling params.
    assert all("temperature" not in c for c in wire)


def test_provider_errors_become_llm_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kwargs: Any) -> Any:
        raise litellm.exceptions.APIConnectionError(
            message="down", llm_provider="anthropic", model=kwargs["model"]
        )

    monkeypatch.setattr(litellm, "completion", boom)
    with pytest.raises(LLMError, match="down"):
        LiteLLMClient().complete(model="anthropic/claude-opus-5-5", system="s", prompt="p")


def test_empty_reply_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real = litellm.completion

    def empty(**kw: Any) -> Any:
        resp = real(**kw, mock_response="x")  # "" would mean "no mock" to LiteLLM
        resp.choices[0].message.content = None
        return resp

    monkeypatch.setattr(litellm, "completion", empty)
    with pytest.raises(LLMError, match="empty"):
        LiteLLMClient().complete(model="anthropic/claude-opus-5-5", system="s", prompt="p")


def test_provider_outage_mid_review_withholds_rather_than_ships(
    monkeypatch: pytest.MonkeyPatch, wire: list[dict[str, Any]]
) -> None:
    """Specialists answer, then the critic's provider fails: nothing unjudged ships."""
    inner = litellm.completion

    def flaky(**kwargs: Any) -> Any:
        if kwargs["model"].endswith("opus-5-5"):  # the critic model
            raise litellm.exceptions.APIConnectionError(
                message="critic down", llm_provider="anthropic", model=kwargs["model"]
            )
        return inner(**kwargs)

    monkeypatch.setattr(litellm, "completion", flaky)
    diff = DiffContext(files=[ChangedFile(path="lib/a.py", patch="+os.system(cmd)\n")])
    result = review_diff(diff, ScrutaiConfig(llm_mode="live"), LiteLLMClient())
    assert result.findings == []
    assert result.dropped and all(f.unjudged for f in result.dropped)
