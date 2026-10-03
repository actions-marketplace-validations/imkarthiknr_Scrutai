"""One import for both MCP Python SDK majors.

mcp 2.x renamed `FastMCP` to `MCPServer`; the decorator API (`tool`, `resource`,
`prompt`, `Context.report_progress`, `Context.elicit`) kept its shape. CrewAI
pins mcp 1.28, so Scrutai supports both and nothing else imports `mcp.server`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel

try:  # mcp >= 2
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver import MCPServer as Server
    from mcp.server.mcpserver.exceptions import ResourceNotFoundError as ResourceError
    from mcp.server.mcpserver.exceptions import ToolError

    MCP_MAJOR = 2
    from mcp_types import ElicitRequest, ElicitRequestFormParams, InputRequiredResult
    from mcp_types.version import MODERN_PROTOCOL_VERSIONS
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import Context
    from mcp.server.fastmcp import FastMCP as Server
    from mcp.server.fastmcp.exceptions import ResourceError, ToolError

    MCP_MAJOR = 1


F = TypeVar("F", bound=Callable[..., Any])


def tool_annotations(
    *, read_only: bool, destructive: bool, idempotent: bool, open_world: bool
) -> ToolAnnotations:
    # By wire name: mcp 1.x silently ignores the snake_case keywords 2.x accepts.
    return ToolAnnotations.model_validate(
        {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": idempotent,
            "openWorldHint": open_world,
        }
    )


def tool(server: Any, **options: Any) -> Callable[[F], F]:
    """`server.tool(...)`, typed so decorated functions keep their signatures."""
    return cast(Callable[[F], F], server.tool(**options))


class ElicitationUnsupported(Exception):
    """The client cannot ask its user anything."""


@dataclass
class Answer:
    """The user's reply to `ask_user`."""

    data: dict[str, Any] | None = None  # the accepted form; None if declined or dismissed
    # mcp 2 on protocol 2026-07-28 has no server-to-client requests: the tool
    # returns this instead, the client asks the user and calls the tool again.
    round_trip: Any = None


_ASK = "scrutai.confirm"


def _can_elicit(ctx: Any) -> bool:
    if MCP_MAJOR == 2:
        caps = ctx.client_capabilities
    else:
        params = ctx.session.client_params
        caps = params.capabilities if params else None
    return bool(caps and caps.elicitation)


async def ask_user(ctx: Any, message: str, schema: type[BaseModel]) -> Answer:
    """Ask the client's user to fill `schema` (elicitation), on either SDK major."""
    if not _can_elicit(ctx):
        raise ElicitationUnsupported
    if MCP_MAJOR == 2 and ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
        response = (ctx.input_responses or {}).get(_ASK)
        if response is None:  # first round: ask
            request = ElicitRequest(
                params=ElicitRequestFormParams(
                    message=message, requested_schema=schema.model_json_schema()
                )
            )
            return Answer(round_trip=InputRequiredResult(input_requests={_ASK: request}))
        accepted = getattr(response, "action", None) == "accept"
        content = getattr(response, "content", None)
        return Answer(data=dict(content) if accepted and content else None)
    result = await ctx.elicit(message, schema)
    if result.action == "accept" and result.data is not None:
        return Answer(data=result.data.model_dump())
    return Answer()


def resource(server: Any, uri: str, **options: Any) -> Callable[[F], F]:
    """`server.resource(...)`, typed like `tool`."""
    return cast(Callable[[F], F], server.resource(uri, **options))


def new_server(name: str, instructions: str, security: TransportSecuritySettings) -> Any:
    """A server instance; `security` is only used by the HTTP transport."""
    if MCP_MAJOR == 2:
        return Server(name, instructions=instructions)
    return Server(name, instructions=instructions, transport_security=security)


def http_app(server: Any, security: TransportSecuritySettings, host: str) -> Any:
    """The Streamable HTTP ASGI app, served at /mcp."""
    if MCP_MAJOR == 2:
        return server.streamable_http_app(transport_security=security, host=host)
    return server.streamable_http_app()


__all__ = [
    "Answer",
    "ElicitationUnsupported",
    "ask_user",
    "MCP_MAJOR",
    "Context",
    "ResourceError",
    "Server",
    "ToolAnnotations",
    "tool_annotations",
    "tool",
    "ToolError",
    "TransportSecuritySettings",
    "http_app",
    "new_server",
    "resource",
]
