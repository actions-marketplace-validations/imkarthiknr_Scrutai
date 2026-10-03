"""Version-neutral MCP client helpers for the tests (mcp 1.x and 2.x)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from mcp import ClientSession

from scrutai.mcp.compat import MCP_MAJOR


@contextlib.asynccontextmanager
async def in_memory(server: Any) -> AsyncIterator[Any]:
    """A client connected to `server` in-process."""
    if MCP_MAJOR == 2:
        from mcp import Client  # type: ignore[attr-defined]

        async with Client(server) as client:
            yield client
    else:
        from mcp.shared.memory import create_connected_server_and_client_session as connect

        async with connect(server._mcp_server) as client:
            yield client


@contextlib.asynccontextmanager
async def over(read: Any, write: Any) -> AsyncIterator[Any]:
    async with ClientSession(read, write) as session:
        await session.initialize()
        yield session


@contextlib.asynccontextmanager
async def over_http(url: str, headers: dict[str, str]) -> AsyncIterator[Any]:
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    async with (
        create_mcp_http_client(headers=headers) as http,
        streamable_http_client(url, http_client=http) as (read, write, *_),
        over(read, write) as session,
    ):
        yield session


def structured(result: Any) -> dict[str, Any]:
    data = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    assert isinstance(data, dict), f"no structured content: {text(result)}"
    return data


def is_error(result: Any) -> bool:
    return bool(getattr(result, "is_error", None) or getattr(result, "isError", None))


def text(result: Any) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


def annotations(tool: Any) -> dict[str, Any]:
    dumped: dict[str, Any] = tool.annotations.model_dump(by_alias=True, exclude_none=True)
    return dumped


def output_schema(tool: Any) -> dict[str, Any] | None:
    schema = getattr(tool, "output_schema", None) or getattr(tool, "outputSchema", None)
    return schema if isinstance(schema, dict) else None
