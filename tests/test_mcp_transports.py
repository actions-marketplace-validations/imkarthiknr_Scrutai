"""End to end: a real `scrutai mcp` process over stdio and over Streamable HTTP."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from mcp_util import over, over_http, structured
from typer.testing import CliRunner

from scrutai.cli import app
from scrutai.demo import DEMO_FILE, DEMO_SOURCE
from scrutai.mcp.security import TOKEN_ENV

pytestmark = pytest.mark.anyio

SCRUTAI = str(Path(sys.executable).with_name("scrutai"))
TOKEN = "test-token-0123456789abcdef"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def demo_repo(git_repo: Callable[[dict[str, str]], Path]) -> Path:
    return git_repo({DEMO_FILE: DEMO_SOURCE})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def http_server(demo_repo: Path) -> Iterator[str]:
    port = _free_port()
    env = {**os.environ, TOKEN_ENV: TOKEN}
    proc = subprocess.Popen(
        [SCRUTAI, "mcp", "--transport", "http", "--port", str(port), "--root", str(demo_repo)],
        cwd=demo_repo,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(proc.stderr.read().decode() if proc.stderr else "exited")
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        else:
            raise RuntimeError("server did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def test_stdio_end_to_end(demo_repo: Path) -> None:
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=SCRUTAI, args=["mcp", "--root", str(demo_repo)], cwd=str(demo_repo)
    )
    async with stdio_client(params) as (read, write), over(read, write) as session:
        names = {t.name for t in (await session.list_tools()).tools}
        assert {"review_patch", "review_git_range"} <= names
        result = await session.call_tool("review_git_range", {"base": "main", "head": "feature"})
    data = structured(result)
    assert data["verdict"] == "request_changes"
    assert "injection" in {f["category"] for f in data["findings"]}


async def test_http_end_to_end_with_a_token(http_server: str) -> None:
    async with over_http(f"{http_server}/mcp", {"Authorization": f"Bearer {TOKEN}"}) as session:
        result = await session.call_tool("review_git_range", {"base": "main", "head": "feature"})
    data = structured(result)
    assert data["verdict"] == "request_changes" and data["label"] == "main...feature"


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 401),
        ({"Authorization": "Bearer wrong-token-0123456789"}, 401),
        ({"Authorization": TOKEN}, 401),  # no "Bearer " scheme
        ({"Authorization": f"Bearer {TOKEN}", "Host": "evil.example"}, 421),
    ],
)
def test_http_rejects_bad_tokens_and_hosts(
    http_server: str, headers: dict[str, str], status: int
) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    response = httpx.post(f"{http_server}/mcp", json=body, headers=headers)
    assert response.status_code == status
    if status == 401:
        assert response.headers["www-authenticate"].startswith("Bearer")


def test_cli_refuses_a_public_bind_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    result = CliRunner().invoke(app, ["mcp", "--transport", "http", "--host", "0.0.0.0"])
    assert result.exit_code == 2 and "needs a bearer token" in result.output


def test_cli_refuses_a_short_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "short")
    result = CliRunner().invoke(app, ["mcp", "--transport", "http"])
    assert result.exit_code == 2 and "at least 16 characters" in result.output


def test_cli_refuses_a_missing_root() -> None:
    result = CliRunner().invoke(app, ["mcp", "--root", "/definitely/not/here"])
    assert result.exit_code == 2 and "not a directory" in result.output
