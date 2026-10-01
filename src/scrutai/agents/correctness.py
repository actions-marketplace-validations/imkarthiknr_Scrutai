from __future__ import annotations

from ..models import DiffContext
from .base import Specialist


class CorrectnessAgent(Specialist):
    name = "correctness"
    role = "You find logic bugs, unhandled None, broad excepts, and edge cases."

    def gather_context(self, diff: DiffContext) -> str:
        patches = "\n".join(f.patch for f in diff.files)
        return f"[role: correctness]\nDIFF:\n{patches}"
