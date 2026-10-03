"""Cancellation and progress: the core hooks the MCP server builds on."""

from __future__ import annotations

import threading

from scrutai import review_diff
from scrutai.config import ScrutaiConfig
from scrutai.llm import MockLLMClient
from scrutai.models import ChangedFile, DiffContext, Verdict
from scrutai.progress import ProgressListener
from scrutai.trace import Tracer

DIFF = DiffContext(
    files=[
        ChangedFile(
            path="lib/run.py",
            patch="+def run(cmd, opts=[]):\n+    try:\n+        os.system(cmd)\n"
            "+    except Exception:\n+        print(cmd)\n",
        )
    ]
)


class Counting(MockLLMClient):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        self.calls += 1
        return super().complete(model=model, system=system, prompt=prompt)


def test_cancel_before_start_makes_no_model_calls() -> None:
    cancel = threading.Event()
    cancel.set()
    llm = Counting()
    result = review_diff(DIFF, ScrutaiConfig(), llm, cancel=cancel)
    assert llm.calls == 0
    assert result.cancelled and result.findings == []
    assert "partial" in result.summary
    assert result.verdict == Verdict.COMMENT  # never "approve" when nothing was reviewed


def test_cancel_mid_review_ships_nothing_unjudged() -> None:
    cancel = threading.Event()

    def stop_after_first_specialist(event: dict[str, object]) -> None:
        if event.get("name") == "specialist" and event.get("phase") == "end":
            cancel.set()

    full = review_diff(DIFF, ScrutaiConfig(), MockLLMClient())
    llm = Counting()
    result = review_diff(
        DIFF, ScrutaiConfig(), llm, Tracer(listeners=[stop_after_first_specialist]), cancel
    )
    assert result.cancelled
    assert len(result.findings) < len(full.findings)
    assert all(not f.unjudged for f in result.findings)


def test_unset_cancel_changes_nothing() -> None:
    plain = review_diff(DIFF, ScrutaiConfig(), MockLLMClient())
    with_flag = review_diff(DIFF, ScrutaiConfig(), MockLLMClient(), cancel=threading.Event())
    assert not with_flag.cancelled
    assert [f.key() for f in with_flag.findings] == [f.key() for f in plain.findings]


def test_progress_is_monotonic_and_completes_once() -> None:
    seen: list[tuple[float, float, str]] = []
    listener = ProgressListener(lambda d, t, m: seen.append((d, t, m)))
    review_diff(DIFF, ScrutaiConfig(), MockLLMClient(), Tracer(listeners=[listener]))
    dones = [d for d, _, _ in seen]
    assert dones == sorted(dones)
    assert seen[0][2].startswith("Routed")
    last_done, last_total, last_msg = seen[-1]
    assert (last_done, last_msg) == (last_total, "Verdict reached")
    assert sum(1 for d, t, _ in seen if d == t) == 1  # reaches 100% exactly once
    assert last_total >= 4  # the plan's tasks + critic + verdict


def test_progress_for_a_docs_only_change() -> None:
    seen: list[tuple[float, float, str]] = []
    docs = DiffContext(files=[ChangedFile(path="README.md", patch="+hello\n")])
    review_diff(
        docs,
        ScrutaiConfig(),
        MockLLMClient(),
        Tracer(listeners=[ProgressListener(lambda d, t, m: seen.append((d, t, m)))]),
    )
    assert seen[0] == (0.0, 2.0, "Routed 0 task(s) to nobody (nothing to review)")
    assert seen[-1] == (2.0, 2.0, "Verdict reached")


def test_partial_budget_review_is_not_an_approval() -> None:
    result = review_diff(DIFF, ScrutaiConfig(token_budget=1), MockLLMClient())
    assert result.budget_exhausted and result.verdict != Verdict.APPROVE


def test_result_event_records_partial_reviews() -> None:
    events: list[dict[str, object]] = []
    review_diff(
        DIFF, ScrutaiConfig(token_budget=1), MockLLMClient(), Tracer(listeners=[events.append])
    )
    (result,) = [e["result"] for e in events if e["kind"] == "result"]
    assert result["budget_exhausted"] is True  # type: ignore[index]
