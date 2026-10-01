"""Real repository tools the specialist ReAct agents call.

These are the "grounded in the real repo, not just the diff" part of Scrutai:
plain, deterministic functions over the working tree and git history. They need
no LLM and are unit-testable on their own.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run(args: list[str], cwd: str = ".") -> str:
    try:
        out = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=30, check=False
        )
        return out.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


def read_file(path: str, repo_root: str = ".") -> str:
    """Return the full text of a file in the repo (empty string if missing)."""
    p = Path(repo_root) / path
    return p.read_text(errors="replace") if p.is_file() else ""


def grep(pattern: str, repo_root: str = ".") -> list[str]:
    """Search the repo. Prefers ripgrep, falls back to git grep."""
    out = _run(["rg", "-n", "--no-heading", pattern, "."], cwd=repo_root)
    if not out:
        out = _run(["git", "grep", "-n", pattern], cwd=repo_root)
    return [line for line in out.splitlines() if line.strip()]


def git_blame(path: str, line: int, repo_root: str = ".") -> str:
    """Blame a single line so an agent can see who/when introduced context."""
    out = _run(
        ["git", "blame", "-L", f"{line},{line}", "--", path], cwd=repo_root
    )
    return out.strip()


def changed_files(base: str, head: str, repo_root: str = ".") -> list[str]:
    """Files touched between two refs."""
    out = _run(["git", "diff", "--name-only", f"{base}...{head}"], cwd=repo_root)
    return [line for line in out.splitlines() if line.strip()]
