"""Small helpers for running independent LLM work concurrently.

LLM calls are I/O-bound, so threads are enough. Results always come back in
input order so the pipeline stays deterministic.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor


def parallel_map[T, R](fn: Callable[[T], R], items: Sequence[T], workers: int) -> list[R]:
    if workers <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(fn, items))
