"""Real repository tools the specialist ReAct agents call.

These are the "grounded in the real repo, not just the diff" part of Scrutai:
plain, deterministic functions over the working tree and git history. They need
no LLM and are unit-testable on their own.

Arguments to these functions can come from a model, so every path is confined
to the repo root (a model must not be able to read `../../.ssh/id_rsa` and ship
it to a provider) and every pattern is passed as data, never as a flag.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_MAX_GREP_HITS = 50


def _run(args: list[str], cwd: str = ".") -> str:
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30, check=False)
        return out.stdout
    except (subprocess.SubprocessError, FileNotFoundError, NotADirectoryError):
        return ""


def _confine(path: str, repo_root: str) -> Path | None:
    """Resolve `path` inside `repo_root`, or None if it escapes it."""
    root = Path(repo_root).resolve()
    target = (root / path).resolve()
    return target if target.is_relative_to(root) else None


def read_file(
    path: str, repo_root: str = ".", start: int | None = None, end: int | None = None
) -> str:
    """Return a file's text (optionally lines start..end, 1-based inclusive).

    Empty string if the file is missing or outside the repo.
    """
    p = _confine(path, repo_root)
    if p is None or not p.is_file():
        return ""
    text = p.read_text(errors="replace")
    if start is None and end is None:
        return text
    lines = text.splitlines()
    lo = max((start or 1) - 1, 0)
    hi = min(end or len(lines), len(lines))
    return "\n".join(f"{i + 1}: {lines[i]}" for i in range(lo, hi))


def grep(pattern: str, repo_root: str = ".", regex: bool = False) -> list[str]:
    """Search the repo; returns `path:line:text` hits. Prefers ripgrep, falls back to git grep.

    Fixed-string by default so model-supplied text like `eval(` is not a broken regex.
    """
    mode = [] if regex else ["-F"]
    out = _run(["rg", "-n", "--no-heading", *mode, "-e", pattern, "."], cwd=repo_root)
    if not out:
        git_mode = ["-E"] if regex else ["-F"]
        out = _run(["git", "grep", "-n", *git_mode, "-e", pattern], cwd=repo_root)
    hits = [line.removeprefix("./") for line in out.splitlines() if line.strip()]
    return hits[:_MAX_GREP_HITS]


def git_blame(path: str, line: int, repo_root: str = ".") -> str:
    """Blame a single line so an agent can see who/when introduced context."""
    if _confine(path, repo_root) is None or line < 1:
        return ""
    out = _run(["git", "blame", "-L", f"{line},{line}", "--", path], cwd=repo_root)
    return out.strip()


def changed_files(base: str, head: str, repo_root: str = ".") -> list[str]:
    """Files touched between two refs."""
    out = _run(["git", "diff", "--name-only", f"{base}...{head}"], cwd=repo_root)
    return [line for line in out.splitlines() if line.strip()]
