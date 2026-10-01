from __future__ import annotations

from typing import ClassVar

from .base import Specialist


class TestCoverageAgent(Specialist):
    __test__ = False  # not a pytest test class, despite the name
    name = "tests"
    role = (
        "You check whether new public functions are exercised by tests. grep the repo for "
        "each new function name before claiming it is untested; tests added in this same "
        "diff count."
    )
    categories: ClassVar[dict[str, str]] = {
        "missing_tests": "a new public function with no test referencing it",
    }
    # Sees test files too, so tests added in the same diff count as coverage.
    kinds = ("code", "test")
    tools: ClassVar[list[str]] = ["grep", "read_file"]
