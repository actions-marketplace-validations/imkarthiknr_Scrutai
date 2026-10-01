"""Turn a git range (or a patch file) into a DiffContext the pipeline can consume."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .models import ChangedFile, DiffContext
from .patch import path_selected, split_unified_diff


class DiffError(RuntimeError):
    """The diff could not be produced (bad ref, not a repo, unreadable file)."""


def parse_diff(text: str, repo_root: str = ".", base: str = "", head: str = "") -> DiffContext:
    files = [
        ChangedFile(path=path, status=status, patch=patch)
        for path, status, patch in split_unified_diff(text)
    ]
    return DiffContext(repo_root=repo_root, base_ref=base, head_ref=head, files=files)


def diff_from_git(base: str, head: str, repo_root: str = ".") -> DiffContext:
    """Diff `base...head` (merge-base semantics, like a PR) in one git call."""
    try:
        proc = subprocess.run(
            ["git", "diff", "--no-color", "--no-ext-diff", "-M", f"{base}...{head}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, NotADirectoryError) as exc:  # git missing / bad cwd
        raise DiffError(str(exc)) from exc
    if proc.returncode != 0:
        # Fail loudly: silently reviewing an empty diff would report "clean".
        raise DiffError(proc.stderr.strip() or f"git diff {base}...{head} failed")
    return parse_diff(proc.stdout, repo_root=repo_root, base=base, head=head)


def diff_from_file(path: str, repo_root: str = ".") -> DiffContext:
    """Read a unified diff from a file, or from stdin when path is `-`."""
    if path == "-":
        text = sys.stdin.read()
    else:
        p = Path(path)
        if not p.is_file():
            raise DiffError(f"no such diff file: {path}")
        text = p.read_text(errors="replace")
    return parse_diff(text, repo_root=repo_root)


def apply_filters(diff: DiffContext, include: list[str], exclude: list[str]) -> DiffContext:
    """Drop files the config excludes, plus deletions and binaries (nothing to review)."""
    kept = [
        f
        for f in diff.files
        if f.status not in ("deleted", "binary") and path_selected(f.path, include, exclude)
    ]
    return diff.model_copy(update={"files": kept})
