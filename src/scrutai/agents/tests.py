from __future__ import annotations

from ..models import DiffContext
from ..tools import grep
from .base import Specialist


class TestCoverageAgent(Specialist):
    name = "tests"
    role = "You check whether new/changed code paths are covered by tests."

    def gather_context(self, diff: DiffContext) -> str:
        existing_tests = grep("def test_", diff.repo_root)[:5]
        patches = "\n".join(f.patch for f in diff.files)
        tests = "\n".join(existing_tests) or "no existing tests found"
        return f"[role: tests]\nDIFF:\n{patches}\n\nEXISTING TESTS:\n{tests}"
