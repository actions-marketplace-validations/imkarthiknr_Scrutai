"""The PR flow end to end, against an in-process fake of the GitHub REST API."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from scrutai.cli import app
from scrutai.github import SUMMARY_MARKER, GitHubClient, GitHubError, detect_repo

REPO = "acme/widgets"
DIFF_V1 = """\
diff --git a/svc/run.py b/svc/run.py
new file mode 100644
--- /dev/null
+++ b/svc/run.py
@@ -0,0 +1,6 @@
+import os
+
+
+def _go(cmd):
+    # careful: os.system(cmd) below
+    return os.system(cmd)
"""
# Second push: two lines inserted above, so the same finding moves from L6 to L8,
# plus a brand-new issue.
DIFF_V2 = """\
diff --git a/svc/run.py b/svc/run.py
new file mode 100644
--- /dev/null
+++ b/svc/run.py
@@ -0,0 +1,9 @@
+import os
+import pickle
+
+
+def _go(cmd):
+    # careful: os.system(cmd) below
+    return os.system(cmd)
+
+state = pickle.loads(blob)
"""


class FakeGitHub:
    def __init__(self) -> None:
        self.diff = DIFF_V1
        self.head = "sha1"
        self.issue_comments: list[dict[str, Any]] = []
        self.review_comments: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self.next_id = 1
        self.auth: set[str] = set()

    def handler(self) -> type[BaseHTTPRequestHandler]:
        gh = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _send(self, code: int, payload: Any, raw: bool = False) -> None:
                body = payload.encode() if raw else json.dumps(payload).encode()
                self.send_response(code)
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> Any:
                n = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(n)) if n else None

            def do_GET(self) -> None:
                gh.auth.add(self.headers.get("Authorization", ""))
                path = self.path.split("?")[0]
                page = 1 if "page=1" in self.path or "page=" not in self.path else 2
                if path == f"/repos/{REPO}/pulls/7":
                    if "diff" in self.headers.get("Accept", ""):
                        return self._send(200, gh.diff, raw=True)
                    return self._send(200, {"head": {"sha": gh.head}, "base": {"ref": "main"}})
                if path == f"/repos/{REPO}/issues/7/comments":
                    return self._send(200, gh.issue_comments if page == 1 else [])
                if path == f"/repos/{REPO}/pulls/7/comments":
                    return self._send(200, gh.review_comments if page == 1 else [])
                self._send(404, {"message": "Not Found"})

            def do_POST(self) -> None:
                body = self._body()
                if self.path == f"/repos/{REPO}/issues/7/comments":
                    gh.issue_comments.append({"id": gh.next_id, "body": body["body"]})
                    gh.next_id += 1
                    return self._send(201, {})
                if self.path == f"/repos/{REPO}/pulls/7/reviews":
                    gh.reviews.append(body)
                    for c in body["comments"]:
                        assert c["side"] == "RIGHT" and c["line"] >= 1
                        gh.review_comments.append({"id": gh.next_id, **c})
                        gh.next_id += 1
                    return self._send(200, {})
                self._send(404, {"message": "Not Found"})

            def do_PATCH(self) -> None:
                body = self._body()
                cid = int(self.path.rsplit("/", 1)[1])
                for c in gh.issue_comments:
                    if c["id"] == cid:
                        c["body"] = body["body"]
                        return self._send(200, {})
                self._send(404, {"message": "Not Found"})

        return H


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHub]:
    fake = FakeGitHub()
    server = HTTPServer(("127.0.0.1", 0), fake.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("GITHUB_API_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    yield fake
    server.shutdown()


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
