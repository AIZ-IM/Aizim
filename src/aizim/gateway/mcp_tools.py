from __future__ import annotations

from typing import Protocol

from jsonschema import ValidationError, validate
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server

from aizim.domain import AgentRole, canonical_json
from aizim.domain.serialization import JsonValue

from .capabilities import (
    GatewayError,
    GatewayFailure,
    GatewaySuccess,
    GatewayTool,
    advertised_tools,
)
from .mcp_schemas import input_schema
from .transport_frames import GatewayResult, result_document


class GatewayMcpChannel(Protocol):
    role: AgentRole

    async def call(
        self, operation: GatewayTool | str, payload: dict[str, JsonValue]
    ) -> GatewayResult: ...


class GatewayChannel(GatewayMcpChannel, Protocol):
    async def wait_disconnected(self) -> None: ...

    async def aclose(self) -> None: ...


def create_gateway_server(channel: GatewayMcpChannel) -> Server:
    async def list_tools(
        _context: ServerRequestContext, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        tools = getattr(channel, "tools", advertised_tools(channel.role))
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=tool.value,
                    description="Aizim role-gated gateway operation",
                    input_schema=input_schema(tool),
                )
                for tool in tools
            ]
        )

    async def call_tool(
        _context: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        name, arguments = params.name, params.arguments
        if type(arguments) is not dict or arguments.keys() != {"payload"}:
            return _mcp_result(_invalid_request())
        payload = arguments["payload"]
        if type(payload) is not dict:
            return _mcp_result(_invalid_request())
        try:
            tool = GatewayTool(name)
        except ValueError:
            tool = None
        if tool is not None:
            try:
                validate(arguments, input_schema(tool))
            except ValidationError:
                return _mcp_result(_invalid_request())
        try:
            result = await channel.call(name, payload)
        except Exception:
            result = GatewayFailure(
                GatewayError("GATEWAY_UNAVAILABLE", "gateway is unavailable", None)
            )
        return _mcp_result(result)

    return Server("aizim-gateway", on_list_tools=list_tools, on_call_tool=call_tool)


def _invalid_request() -> GatewayFailure:
    return GatewayFailure(GatewayError("INVALID_REQUEST", "tool arguments are invalid", None))


def _mcp_result(result: GatewaySuccess | GatewayFailure) -> types.CallToolResult:
    document = result_document(result)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=canonical_json(document).decode())],
        structured_content=document,
        is_error=isinstance(result, GatewayFailure),
    )
