"""Shared fixtures: a throwaway git repo with a base and a feature branch."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

pytest_plugins = ["fake_github"]  # the `github` fixture: a fake GitHub REST API


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Callable[[dict[str, str]], Path]:
    """Return a factory: given {path: content}, commit it on `feature` over `main`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# demo\n")
    (repo / "app.py").write_text("def keep():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")

    def make(changes: dict[str, str]) -> Path:
        for rel, content in changes.items():
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "change")
        return repo

    return make
