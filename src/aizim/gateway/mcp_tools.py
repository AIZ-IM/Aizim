from __future__ import annotations

from typing import Protocol

from mcp import types
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
from .transport_frames import GatewayResult, result_document

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"payload": {"type": "object"}},
    "required": ["payload"],
    "additionalProperties": False,
}


class GatewayMcpChannel(Protocol):
    role: AgentRole

    async def call(
        self, operation: GatewayTool | str, payload: dict[str, JsonValue]
    ) -> GatewayResult: ...


class GatewayChannel(GatewayMcpChannel, Protocol):
    async def wait_disconnected(self) -> None: ...

    async def aclose(self) -> None: ...


def create_gateway_server(channel: GatewayMcpChannel) -> Server:
    server = Server("aizim-gateway")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=tool.value,
                description="Aizim role-gated gateway operation",
                inputSchema=_INPUT_SCHEMA,
            )
            for tool in advertised_tools(channel.role)
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, JsonValue]) -> types.CallToolResult:
        if type(arguments) is not dict or arguments.keys() != {"payload"}:
            return _mcp_result(_invalid_request())
        payload = arguments["payload"]
        if type(payload) is not dict:
            return _mcp_result(_invalid_request())
        try:
            result = await channel.call(name, payload)
        except Exception:
            result = GatewayFailure(
                GatewayError("GATEWAY_UNAVAILABLE", "gateway is unavailable", None)
            )
        return _mcp_result(result)

    return server


def _invalid_request() -> GatewayFailure:
    return GatewayFailure(GatewayError("INVALID_REQUEST", "tool arguments are invalid", None))


def _mcp_result(result: GatewaySuccess | GatewayFailure) -> types.CallToolResult:
    document = result_document(result)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=canonical_json(document).decode())],
        structuredContent=document,
        isError=isinstance(result, GatewayFailure),
    )
