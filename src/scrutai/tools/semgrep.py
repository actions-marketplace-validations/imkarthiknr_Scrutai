"""Semgrep as a specialist tool.

SAST results are the strongest evidence a security finding can carry: a
deterministic rule fired on the exact line. Scrutai runs Semgrep only when it is
installed (it is an optional dependency), restricts results to lines the diff
adds, and hands them to the security agent as seed observations.

The default ruleset is bundled (`rules/semgrep.yml`) so this works offline; set
`semgrep_config` to a registry pack such as `p/default` for broader coverage.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BUNDLED_RULES = str(Path(__file__).resolve().parent.parent / "rules" / "semgrep.yml")

# Fallback mapping for registry rule ids that don't embed a Scrutai category.
_CATEGORY_HINTS = (
    ("sql", "sql_injection"),
    ("pickle", "unsafe_deserialization"),
    ("yaml", "unsafe_deserialization"),
    ("deserial", "unsafe_deserialization"),
    ("secret", "hardcoded_secret"),
    ("password", "hardcoded_secret"),
    ("md5", "weak_crypto"),
    ("sha1", "weak_crypto"),
    ("verify", "tls_verify_disabled"),
    ("path-traversal", "path_traversal"),
    ("exec", "injection"),
    ("eval", "injection"),
    ("shell", "injection"),
    ("command", "injection"),
    ("subprocess", "injection"),
)


@dataclass(frozen=True)
class SemgrepHit:
    rule_id: str
    path: str
    line: int
    severity: str
    message: str

    @property
    def category(self) -> str:
        return category_for(self.rule_id)

    def render(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule_id}] ({self.severity}) {self.message}"


def normalize_rule_id(rule_id: str) -> str:
    """Strip the path prefix Semgrep adds to local rule ids.

    A bundled rule `scrutai.injection.os-system` is reported as e.g.
    `home.me.venv.scrutai.rules.scrutai.injection.os-system`.
    """
    i = rule_id.rfind("scrutai.")
    return rule_id[i:] if i > 0 and rule_id[i - 1] == "." else rule_id


def category_for(rule_id: str) -> str:
    parts = normalize_rule_id(rule_id).split(".")
    if len(parts) >= 3 and parts[0] == "scrutai":
        return parts[1]
    lowered = rule_id.lower()
    return next((cat for hint, cat in _CATEGORY_HINTS if hint in lowered), "sast")


def available() -> bool:
    return shutil.which("semgrep") is not None


def parse_output(text: str) -> list[SemgrepHit]:
    """Parse `semgrep --json` output; tolerant of missing fields."""
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        return []
    hits: list[SemgrepHit] = []
    for r in data.get("results", []) if isinstance(data, dict) else []:
        try:
            extra = r.get("extra", {})
            hits.append(
                SemgrepHit(
                    rule_id=normalize_rule_id(str(r["check_id"])),
                    path=str(r["path"]).removeprefix("./"),
                    line=int(r["start"]["line"]),
                    severity=str(extra.get("severity", "INFO")),
                    message=str(extra.get("message", "")).strip(),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return hits


def scan(paths: list[str], repo_root: str = ".", config: str = BUNDLED_RULES) -> list[SemgrepHit]:
    """Run Semgrep over `paths` (relative to repo_root). [] if unavailable or it fails."""
    if not paths or not available():
        return []
    cfg = BUNDLED_RULES if config in ("", "bundled") else config
    try:
        proc = subprocess.run(
            [
                "semgrep",
                "scan",
                "--json",
                "--quiet",
                "--metrics=off",
                "--config",
                cfg,
                "--",
                *paths,
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    return parse_output(proc.stdout)
