from __future__ import annotations

import pytest

from scrutai import review_diff
from scrutai.config import ScrutaiConfig
from scrutai.llm import BudgetedClient, BudgetExceeded, MockLLMClient
from scrutai.models import ChangedFile, DiffContext

DIFF = DiffContext(files=[ChangedFile(path="lib/a.py", patch="+def f(x=[]):\n+    os.system(x)\n")])


class Priced(MockLLMClient):
    """Mock that charges $0.01 per call."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    @property
    def cost_usd(self) -> float:
        return self.calls * 0.01

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        self.calls += 1
        return super().complete(model=model, system=system, prompt=prompt)


def test_budgeted_client_stops_after_budget() -> None:
    client = BudgetedClient(MockLLMClient(), token_budget=1)
    client.complete(model="m", system="s", prompt="ROLE: style\n")  # first call allowed
    with pytest.raises(BudgetExceeded):
        client.complete(model="m", system="s", prompt="ROLE: style\n")
    assert client.exhausted


def test_zero_disables_budget() -> None:
    client = BudgetedClient(MockLLMClient(), token_budget=0)
    for _ in range(5):
        client.complete(model="m", system="s", prompt="x" * 1000)
    assert not client.exhausted


def test_tiny_budget_yields_partial_review_not_a_crash() -> None:
    full = review_diff(DIFF, ScrutaiConfig(), MockLLMClient())
    partial = review_diff(DIFF, ScrutaiConfig(token_budget=500), MockLLMClient())
    assert not full.budget_exhausted and full.findings
    assert partial.budget_exhausted
    assert "partial" in partial.summary
    assert partial.tokens_used < full.tokens_used
    # Anything raised but never cross-examined is withheld, not shipped.
    assert all(not f.unjudged for f in partial.findings)


def test_dollar_cap() -> None:
    llm = Priced()
    result = review_diff(DIFF, ScrutaiConfig(token_budget=0, max_cost_usd=0.03), llm)
    assert result.budget_exhausted
    assert llm.calls <= 3 + ScrutaiConfig().concurrency  # soft cap: bounded overshoot
    assert result.cost_usd == pytest.approx(llm.calls * 0.01)
