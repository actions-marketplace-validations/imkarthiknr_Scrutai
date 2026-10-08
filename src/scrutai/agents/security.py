from __future__ import annotations

from typing import ClassVar

from ..models import DiffContext, Finding
from ..tools import Toolbox, semgrep
from .base import Specialist


class SecurityAgent(Specialist):
    name = "security"
    role = (
        "You hunt for injection, hardcoded secrets, unsafe deserialization, weak crypto "
        "and disabled transport security. Before reporting, use grep/read_file to check "
        "whether input is actually untrusted or already sanitized."
    )
    categories: ClassVar[dict[str, str]] = {
        "injection": "untrusted input reaches a shell, eval/exec, or similar sink",
        "sql_injection": "SQL assembled with string formatting instead of parameters",
        "hardcoded_secret": "a real credential or key committed in source",
        "unsafe_deserialization": "pickle/yaml.load/marshal on data that may be untrusted",
        "weak_crypto": "MD5/SHA1 or similar used where security matters",
        "tls_verify_disabled": "certificate verification turned off",
        "path_traversal": "user-controlled path reaches the filesystem unchecked",
    }
    kinds = ("code", "test")
    tools: ClassVar[list[str]] = ["read_file", "grep", "git_blame", "semgrep"]
    hits: list[semgrep.SemgrepHit]

    def seed(self, diff: DiffContext, toolbox: Toolbox) -> list[str]:
        """Run Semgrep over the changed files; keep only hits on added lines."""
        self.hits = []
        # "required" is enforced at startup (cli.py); here missing just means skip.
        if self.config.semgrep == "off" or not semgrep.available():
            return []
        files = self.files(diff)
        added = {(f.path, d.line) for f in files for d in f.added}
        self.hits = hits = [
            h
            for h in semgrep.scan(
                [f.path for f in files], diff.repo_root, self.config.semgrep_config
            )
            if (h.path, h.line) in added
        ]
        toolbox.calls.append(f"tool:semgrep({len(files)} file(s)) -> {len(hits)} hit(s)")
        if not hits:
            return ["SEMGREP: no rule fired on the added lines."]
        return ["SEMGREP:\n" + "\n".join(h.render() for h in hits)]

    def ground(self, findings: list[Finding]) -> list[Finding]:
        """Record the Semgrep rule that fired on a finding's exact line and category."""
        for f in findings:
            f.sast_rule = next(
                (
                    h.rule_id
                    for h in getattr(self, "hits", [])
                    if (h.path, h.line) == (f.file, f.line) and h.category in (f.category, "sast")
                ),
                None,
            )
        return findings
