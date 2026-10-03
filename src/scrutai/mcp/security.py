"""The MCP server's guard rails: path allowlist, input caps, HTTP authentication.

A remote client controls every tool argument, so nothing it sends is trusted:
repository paths must resolve inside a root the operator allowed, git refs are
validated (in `diff.py`), inputs are capped, and the HTTP transport demands a
bearer token whenever it listens beyond loopback.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any

from ..models import DiffContext

TOKEN_ENV = "SCRUTAI_MCP_TOKEN"
LOOPBACK = ("127.0.0.1", "localhost", "::1")

MAX_PATCH_BYTES = 2_000_000
MAX_FILES = 300
MAX_DIFF_BYTES = 4_000_000


class SecurityError(ValueError):
    """A request the server's policy refuses."""


class Roots:
    """The directories tools may read. A requested repo must resolve inside one."""

    def __init__(self, roots: list[str]) -> None:
        resolved = [Path(r).expanduser().resolve() for r in roots or ["."]]
        missing = [str(r) for r in resolved if not r.is_dir()]
        if missing:
            raise SecurityError(f"root is not a directory: {', '.join(missing)}")
        self.paths = resolved

    @property
    def default(self) -> Path:
        return self.paths[0]

    def resolve(self, repo: str | None) -> Path:
        """`repo` (absolute, or relative to the first root) as a real path inside a root."""
        if not repo:
            return self.default
        candidate = Path(repo).expanduser()
        if not candidate.is_absolute():
            candidate = self.default / candidate
        real = candidate.resolve()  # follows symlinks: a link out of a root is refused
        if not any(real == root or real.is_relative_to(root) for root in self.paths):
            raise SecurityError(f"repo {repo!r} is outside the allowed roots")
        if not real.is_dir():
            raise SecurityError(f"repo {repo!r} is not a directory")
        return real


def check_patch(patch: str) -> None:
    if not patch.strip():
        raise SecurityError("patch is empty")
    if len(patch.encode("utf-8", errors="replace")) > MAX_PATCH_BYTES:
        raise SecurityError(f"patch is larger than {MAX_PATCH_BYTES:,} bytes")


def check_diff(diff: DiffContext) -> None:
    if len(diff.files) > MAX_FILES:
        raise SecurityError(f"diff touches {len(diff.files)} files; the limit is {MAX_FILES}")
    size = sum(len(f.patch) for f in diff.files)
    if size > MAX_DIFF_BYTES:
        raise SecurityError(f"diff is larger than {MAX_DIFF_BYTES:,} bytes")


def http_token(host: str) -> str:
    """The bearer token for HTTP, from the environment only (never a CLI flag).

    Listening beyond loopback without one is refused: the server reads your
    code and spends your model budget.
    """
    token = os.environ.get(TOKEN_ENV, "")
    if token and len(token) < 16:
        raise SecurityError(f"{TOKEN_ENV} must be at least 16 characters")
    if not token and host not in LOOPBACK:
        raise SecurityError(f"binding to {host} needs a bearer token: set {TOKEN_ENV}")
    return token


def allowed_hosts(host: str, port: int, extra: list[str]) -> list[str]:
    """Host header values to accept (against DNS rebinding)."""
    names = {"127.0.0.1", "localhost", "[::1]"} | set(extra)
    if host not in LOOPBACK and host not in ("0.0.0.0", "::"):
        names.add(host)
    hosts = set(extra)
    for name in names:
        hosts.update({name, f"{name}:{port}"})
    return sorted(hosts)


Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class Guard:
    """ASGI middleware: Host allowlist, then constant-time bearer token check."""

    def __init__(self, app: ASGIApp, token: str, hosts: list[str]) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode() if token else b""
        self.hosts = {h.lower() for h in hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # lifespan
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        host = headers.get(b"host", b"").decode("latin-1").lower()
        if host not in self.hosts:
            await _deny(send, 421, "unexpected Host header")
            return
        if self.expected and not hmac.compare_digest(
            headers.get(b"authorization", b""), self.expected
        ):
            await _deny(send, 401, "missing or invalid bearer token", bearer=True)
            return
        await self.app(scope, receive, send)


async def _deny(send: Send, status: int, message: str, bearer: bool = False) -> None:
    headers = [(b"content-type", b"text/plain; charset=utf-8")]
    if bearer:
        headers.append((b"www-authenticate", b'Bearer realm="scrutai"'))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": message.encode()})
