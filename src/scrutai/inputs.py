"""Where a review's diff comes from: one place for every front end.

The CLI, the web server and the MCP server all accept the same kinds of input
(the bundled demo, a git range, patch text, a patch file, a GitHub pull
request) and must treat them identically: same validation, same filters, same
errors. `prepare()` is that single path.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from .config import ScrutaiConfig
from .demo import demo_diff
from .diff import DiffError, apply_filters, diff_from_file, diff_from_git, parse_diff
from .github import GitHubClient, PullRequest, detect_repo
from .models import DiffContext

SourceKind = Literal["demo", "git", "patch", "file", "pr"]


@dataclass(frozen=True)
class Source:
    """What to review. Only the fields for `kind` are read."""

    kind: SourceKind
    base: str = "main"
    head: str = "HEAD"
    patch: str = ""  # kind="patch": unified diff text
    path: str = ""  # kind="file": a diff file, or "-" for stdin
    pr: int = 0  # kind="pr"
    github_repo: str = ""  # kind="pr": owner/name (default: detected)

    def label(self) -> str:
        return {
            "demo": "Demo: app/runner.py",
            "git": f"{self.base}...{self.head}",
            "patch": "Pasted patch",
            "file": f"Patch file {self.path}",
            "pr": f"PR #{self.pr}",
        }[self.kind]


@dataclass
class Prepared:
    """A filtered diff ready to review, plus what posting back to GitHub needs."""

    diff: DiffContext
    github: GitHubClient | None = None
    pull: PullRequest | None = None


def github_client(repo_root: str = ".", github_repo: str = "") -> GitHubClient:
    """A GitHub client from the environment (GITHUB_TOKEN, GITHUB_API_URL)."""
    return GitHubClient(
        os.environ.get("GITHUB_TOKEN", ""),
        github_repo or detect_repo(repo_root),
        os.environ.get("GITHUB_API_URL", "https://api.github.com"),
    )


@contextlib.contextmanager
def prepare(source: Source, config: ScrutaiConfig, repo_root: str = ".") -> Iterator[Prepared]:
    """Yield the diff for `source`, filtered by the config's include/exclude globs.

    A context manager because some inputs own resources for the review's
    duration (the demo's temporary repository). Raises DiffError or
    GitHubError when the input is invalid or unreachable.
    """
    if source.kind == "demo":
        with demo_diff() as diff:
            yield Prepared(apply_filters(diff, config.include, config.exclude))
        return

    github: GitHubClient | None = None
    pull: PullRequest | None = None
    if source.kind == "git":
        diff = diff_from_git(source.base, source.head, repo_root)
    elif source.kind == "patch":
        if not source.patch.strip():
            raise DiffError("empty patch")
        diff = parse_diff(source.patch, repo_root=repo_root)
    elif source.kind == "file":
        diff = diff_from_file(source.path, repo_root=repo_root)
    elif source.kind == "pr":
        if source.pr <= 0:
            raise DiffError("a pull request number is required")
        github = github_client(repo_root, source.github_repo)
        pull = github.pull_request(source.pr)
        diff = parse_diff(
            github.pull_request_diff(source.pr), repo_root=repo_root, head=pull.head_sha
        )
    else:  # pragma: no cover - Literal exhausts this
        raise DiffError(f"unknown source kind {source.kind!r}")
    yield Prepared(apply_filters(diff, config.include, config.exclude), github, pull)
