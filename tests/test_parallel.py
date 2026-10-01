from __future__ import annotations

import json
import threading
import time

from scrutai import review_diff
from scrutai.concurrency import parallel_map
from scrutai.config import ScrutaiConfig
from scrutai.llm import MockLLMClient
from scrutai.models import ChangedFile, DiffContext

PATCH = (
    "+import os\n"
    "+def run(cmd, opts=[]):\n"
    "+    try:\n"
    "+        os.system(cmd)\n"
    "+    except Exception:\n"
    "+        print(cmd)\n"
)
DIFF = DiffContext(files=[ChangedFile(path="lib/run.py", patch=PATCH)])


class SlowSilent:
    """Every call takes 0.2s and finds nothing; records peak concurrency."""

    cost_usd = 0.0

    def __init__(self) -> None:
        self.active = self.peak = self.tokens_used = 0
        self.lock = threading.Lock()

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.2)
        with self.lock:
            self.active -= 1
        return json.dumps({"findings": []})


def test_specialists_run_concurrently() -> None:
    llm = SlowSilent()
    start = time.perf_counter()
    result = review_diff(DIFF, ScrutaiConfig(), llm)
    elapsed = time.perf_counter() - start
    assert len(result.agents) >= 4
    assert llm.peak >= 3
    assert elapsed < 0.2 * len(result.agents) * 0.75  # well under sequential time


def test_parallel_results_are_deterministic() -> None:
    def snapshot() -> list[tuple[str, int | None, str, str]]:
        r = review_diff(DIFF, ScrutaiConfig(), MockLLMClient())
        return [(f.agent, f.line, f.category, f.severity.value) for f in r.findings]

    first = snapshot()
    assert len(first) >= 4
    assert all(snapshot() == first for _ in range(5))


def test_parallel_map_keeps_order() -> None:
    def slow_square(x: int) -> int:
        time.sleep(0.01 * (5 - x))
        return x * x

    assert parallel_map(slow_square, [1, 2, 3, 4], workers=4) == [1, 4, 9, 16]
    assert parallel_map(slow_square, [3], workers=4) == [9]


def test_no_agents_skips_straight_to_verdict() -> None:
    diff = DiffContext(files=[ChangedFile(path="README.md", patch="+hello\n")])
    result = review_diff(diff, ScrutaiConfig(), MockLLMClient())
    assert result.agents == [] and result.findings == [] and result.tokens_used == 0
