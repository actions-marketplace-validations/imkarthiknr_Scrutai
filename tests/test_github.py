"""The PR flow end to end, against an in-process fake of the GitHub REST API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fake_github import DIFF_V2, REPO, FakeGitHub
from typer.testing import CliRunner

from scrutai.cli import app
from scrutai.github import SUMMARY_MARKER, GitHubClient, GitHubError, detect_repo


def _run(*extra: str) -> Any:
    return CliRunner().invoke(app, ["review", "--pr", "7", "--post", *extra])


def test_pr_review_posts_once_and_updates_in_place(github: FakeGitHub) -> None:
    res = _run()
    assert res.exit_code == 1, res.output  # high-severity injection
    assert "summary created, 1 new inline comment(s)" in res.output
    (summary,) = github.issue_comments
    assert summary["body"].startswith(SUMMARY_MARKER)
    (review,) = github.reviews
    (comment,) = review["comments"]
    assert (comment["path"], comment["line"]) == ("svc/run.py", 6)  # not the comment line
    assert "scrutai:fp=" in comment["body"]
    assert github.auth == {"Bearer t0ken"}

    # Re-run on the same head: nothing new, summary edited not duplicated.
    res = _run()
    assert "summary updated, 0 new inline comment(s), 1 already posted" in res.output
    assert len(github.issue_comments) == 1 and len(github.reviews) == 1

    # New push: the old finding moved to L7; only the pickle finding is new.
    github.diff, github.head = DIFF_V2, "sha2"
    res = _run()
    assert "1 new inline comment(s), 1 already posted" in res.output
    assert github.reviews[-1]["commit_id"] == "sha2"
    (new,) = github.reviews[-1]["comments"]
    assert (new["line"], "Unsafe deserialization" in new["body"]) == (9, True)


def test_pr_without_post_is_read_only(github: FakeGitHub) -> None:
    res = CliRunner().invoke(app, ["review", "--pr", "7", "--json"])
    assert res.exit_code == 1
    assert json.loads(res.output)["findings"][0]["line"] == 6
    assert github.issue_comments == [] and github.reviews == []


def test_api_errors_exit_2(github: FakeGitHub) -> None:
    res = CliRunner().invoke(app, ["review", "--pr", "404"])
    assert res.exit_code == 2 and "HTTP 404" in res.output


def test_post_requires_pr() -> None:
    res = CliRunner().invoke(app, ["review", "--demo", "--post"])
    assert res.exit_code == 2


def test_client_validation() -> None:
    with pytest.raises(GitHubError, match="token"):
        GitHubClient("", REPO)
    with pytest.raises(GitHubError, match="owner/name"):
        GitHubClient("t", "not a repo")


def test_detect_repo_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "a/b")
    assert detect_repo() == "a/b"


def _action_script() -> str:
    import yaml

    action = yaml.safe_load(Path("action.yml").read_text())
    (step,) = [s for s in action["runs"]["steps"] if s.get("id") == "review"]
    return str(step["run"])


@pytest.mark.parametrize(("fail", "expected_code"), [("true", 1), ("false", 0)])
def test_action_script_end_to_end(
    github: FakeGitHub, tmp_path: Any, fail: str, expected_code: int
) -> None:
    """Run the Action's real shell step against the fake API."""
    import os
    import subprocess
    import sys

    out, summary = tmp_path / "out", tmp_path / "summary.md"
    env = {
        **os.environ,
        "PATH": f"{os.path.dirname(sys.executable)}{os.pathsep}{os.environ['PATH']}",
        "PR_NUMBER": "7",
        "CONFIG": ".scrutai.yml",
        "LLM_MODE": "mock",
        "POST": "true",
        "SARIF": str(tmp_path / "r.sarif"),
        "FAIL": fail,
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(out),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    proc = subprocess.run(
        ["bash", "-e", "-c", _action_script()], env=env, capture_output=True, text=True
    )
    assert proc.returncode == expected_code, proc.stdout + proc.stderr
    assert out.read_text().strip() == "verdict=request_changes"
    assert "### Scrutai review" in summary.read_text()
    sarif = json.loads((tmp_path / "r.sarif").read_text())
    assert sarif["runs"][0]["results"][0]["ruleId"] == "injection"
    assert "llm_mode: mock" in (tmp_path / "scrutai.yml").read_text()
    assert len(github.issue_comments) == 1


def test_action_script_requires_a_pr(tmp_path: Any) -> None:
    import os
    import subprocess

    env = {**os.environ, "PR_NUMBER": "", "RUNNER_TEMP": str(tmp_path)}
    proc = subprocess.run(
        ["bash", "-e", "-c", _action_script()], env=env, capture_output=True, text=True
    )
    assert proc.returncode == 2 and "pull_request" in proc.stdout


def test_action_metadata_is_marketplace_ready() -> None:
    import yaml

    action = yaml.safe_load(Path("action.yml").read_text(encoding="utf-8"))
    assert action["name"] and action["author"]
    assert 0 < len(action["description"]) <= 125  # the Marketplace shows a one-line summary
    assert set(action["branding"]) == {"icon", "color"}
