"""An in-process fake of the GitHub REST API (one repo, PR #7), shared by tests."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

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
