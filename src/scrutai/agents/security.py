from __future__ import annotations

from ..models import DiffContext
from ..tools import grep
from .base import Specialist


class SecurityAgent(Specialist):
    name = "security"
    role = (
        "You hunt for injection, secrets, unsafe deserialization, and auth flaws. "
        "In production this agent also shells out to Semgrep; that call is a TODO."
    )

    def gather_context(self, diff: DiffContext) -> str:
        # ReAct 'act': ground the reasoning in the real repo, not just the patch.
        hits: list[str] = []
        for pattern in ("eval(", "os.system", "subprocess", "pickle.loads"):
            hits += grep(pattern, diff.repo_root)[:3]
        patches = "\n".join(f.patch for f in diff.files)
        evidence = "\n".join(hits) or "grep: no known sinks matched"
        # TODO(v0.2): run `semgrep --json` here and fold results into evidence.
        return f"[role: security]\nDIFF:\n{patches}\n\nREPO GREP:\n{evidence}"
