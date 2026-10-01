"""Turn a git range into a DiffContext the pipeline can consume."""

from __future__ import annotations

import subprocess

from .models import ChangedFile, DiffContext


def diff_from_git(base: str, head: str, repo_root: str = ".") -> DiffContext:
    names = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    files: list[ChangedFile] = []
    for path in names:
        patch = subprocess.run(
            ["git", "diff", f"{base}...{head}", "--", path],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        files.append(ChangedFile(path=path, patch=patch))
    return DiffContext(repo_root=repo_root, base_ref=base, head_ref=head, files=files)
