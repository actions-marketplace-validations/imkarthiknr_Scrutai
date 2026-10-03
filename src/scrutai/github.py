"""GitHub pull-request integration (what the Action runs).

Reads a PR's diff from the REST API, and publishes a review idempotently:

* one **summary comment**, found again by a hidden marker and edited in place
  on every push instead of piling up;
* one **inline comment per finding**, on the exact added line, carrying a
  hidden fingerprint. Fingerprints ignore line numbers, so a finding that
  survives a push (even if it moved) is never posted twice.

Plain `urllib`, no SDK: the Action installs nothing beyond Scrutai itself.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .models import DiffContext, ReviewResult
from .report import finding_markdown, to_markdown

SUMMARY_MARKER = "<!-- scrutai:summary -->"
_FP_MARKER = re.compile(r"<!-- scrutai:fp=([0-9a-f]+) -->")


class GitHubError(RuntimeError):
    pass


@dataclass
class PullRequest:
    number: int
    head_sha: str
    base_ref: str


class GitHubClient:
    def __init__(self, token: str, repo: str, api_url: str = "https://api.github.com") -> None:
        if not token:
            raise GitHubError("a GitHub token is required (set GITHUB_TOKEN)")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise GitHubError(f"repository must look like owner/name, got {repo!r}")
        self.token, self.repo, self.api_url = token, repo, api_url.rstrip("/")

    def _request(
        self, method: str, path: str, body: Any = None, accept: str = "application/vnd.github+json"
    ) -> tuple[Any, str]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.api_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": accept,
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "scrutai",
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise GitHubError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise GitHubError(f"{method} {path} failed: {exc.reason}") from exc
        if accept.endswith("json"):
            return (json.loads(text) if text else None), text
        return None, text

    def _paged(self, path: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            sep = "&" if "?" in path else "?"
            batch, _ = self._request("GET", f"{path}{sep}per_page=100&page={page}")
            out.extend(batch or [])
            if not batch or len(batch) < 100:
                return out
            page += 1

    # ---- reads ---------------------------------------------------------------

    def pull_request(self, number: int) -> PullRequest:
        pr, _ = self._request("GET", f"/repos/{self.repo}/pulls/{number}")
        return PullRequest(number, pr["head"]["sha"], pr["base"]["ref"])

    def pull_request_diff(self, number: int) -> str:
        _, text = self._request(
            "GET", f"/repos/{self.repo}/pulls/{number}", accept="application/vnd.github.v3.diff"
        )
        return text

    # ---- writes --------------------------------------------------------------

    def upsert_summary(self, number: int, body: str) -> str:
        body = f"{SUMMARY_MARKER}\n{body}"
        for c in self._paged(f"/repos/{self.repo}/issues/{number}/comments"):
            if SUMMARY_MARKER in (c.get("body") or ""):
                self._request(
                    "PATCH", f"/repos/{self.repo}/issues/comments/{c['id']}", {"body": body}
                )
                return "updated"
        self._request("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})
        return "created"

    def posted_fingerprints(self, number: int) -> set[str]:
        comments = self._paged(f"/repos/{self.repo}/pulls/{number}/comments")
        return {m for c in comments for m in _FP_MARKER.findall(c.get("body") or "")}

    def create_review(
        self, number: int, commit_id: str, body: str, comments: list[dict[str, Any]]
    ) -> None:
        self._request(
            "POST",
            f"/repos/{self.repo}/pulls/{number}/reviews",
            {"commit_id": commit_id, "body": body, "event": "COMMENT", "comments": comments},
        )


@dataclass
class PublishReport:
    summary: str  # "created" | "updated"
    posted: int  # new inline comments
    skipped_duplicates: int
    skipped_off_diff: int


def publish(
    client: GitHubClient, pr: PullRequest, result: ReviewResult, diff: DiffContext
) -> PublishReport:
    summary = client.upsert_summary(pr.number, to_markdown(result))
    already = client.posted_fingerprints(pr.number)
    added = {(f.path, d.line) for f in diff.files for d in f.added}

    comments: list[dict[str, Any]] = []
    dupes = off_diff = 0
    for f in result.findings:
        fp = f.fingerprint()
        if fp in already:
            dupes += 1
            continue
        # GitHub only accepts inline comments on lines that are part of the diff.
        if f.line is None or (f.file, f.line) not in added:
            off_diff += 1
            continue
        comments.append(
            {
                "path": f.file,
                "line": f.line,
                "side": "RIGHT",
                "body": f"{finding_markdown(f)}\n\n<!-- scrutai:fp={fp} -->",
            }
        )
        already.add(fp)
    if comments:
        client.create_review(
            pr.number,
            pr.head_sha,
            f"Scrutai: {len(comments)} new finding(s) survived cross-examination.",
            comments,
        )
    return PublishReport(summary, len(comments), dupes, off_diff)


def detect_repo(repo_root: str = ".") -> str:
    """owner/name from GITHUB_REPOSITORY, else from the `origin` remote URL."""
    env = os.environ.get("GITHUB_REPOSITORY", "")
    if env:
        return env
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",  # not the locale code page (cp1252 on Windows)
            errors="replace",
            check=False,
        ).stdout.strip()
    except OSError:
        url = ""
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", url)
    return m.group(1) if m else ""
