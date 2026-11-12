from __future__ import annotations

import asyncio
import json
from typing import Protocol

from jsonschema import Draft202012Validator

from aizim.domain.serialization import JsonValue
from aizim.gateway.capabilities import (
    GATEWAY_RATE_LIMITED,
    GatewayFailure,
    GatewaySuccess,
    GatewayTool,
)
from aizim.gateway.mcp_schemas import input_schema
from aizim.gateway.transport_frames import GatewayResult, result_document


class GatewayToolChannel(Protocol):
    @property
    def tools(self) -> tuple[GatewayTool, ...]: ...

    async def call(
        self, operation: GatewayTool | str, payload: dict[str, JsonValue]
    ) -> GatewayResult: ...


# The gateway or its transport refused the call. Other failures are ordinary tool results.
_REFUSAL_CODES = frozenset({"CAPABILITY_DENIED", "GATEWAY_INTERNAL", "GATEWAY_PROTOCOL"})


class GatewayHostTools:
    """The only bridge from untrusted RPC tool arguments to trusted authority.

    ``submitted`` records an accepted contribution. ``failed`` records a refusal
    or an unavailable gateway; tool errors such as a rejected edit are returned
    to the agent without failing the worker.
    Gateway rate limits are retried after the trusted delay. Cancellation and the
    enclosing attempt deadline still stop the pending tool call.
    """

    def __init__(self, channel: GatewayToolChannel) -> None:
        self.channel = channel
        self._tools: dict[str, GatewayTool] = {
            tool.value.replace(".", "_"): tool for tool in channel.tools
        }
        if len(self._tools) != len(channel.tools):
            raise ValueError("GATEWAY_TOOL_NAME_COLLISION")
        self._validators = {
            name: Draft202012Validator(input_schema(tool)) for name, tool in self._tools.items()
        }
        self.submitted = False
        self.failed = False

    @property
    def definitions(self) -> list[dict]:
        return [{
            "name": name, "label": name,
            "description": "Aizim role-authorized research gateway operation",
            "parameters": input_schema(tool), "loadMode": "essential",
        } for name, tool in self._tools.items()]

    async def __call__(self, name: str, arguments: object) -> tuple[str, bool]:
        validator = self._validators.get(name)
        if (validator is None or not isinstance(arguments, dict)
                or not validator.is_valid(arguments)):
            return '{"ok":false,"error":{"code":"INVALID_REQUEST"}}', True
        try:
            while True:
                result = await self.channel.call(self._tools[name], arguments["payload"])
                if not (isinstance(result, GatewayFailure)
                        and result.error.code == GATEWAY_RATE_LIMITED):
                    break
                delay = result.error.retry_after_seconds
                assert delay is not None  # GatewayError validates the reserved retry contract.
                await asyncio.sleep(delay)
        except Exception:
            self.failed = True
            return '{"ok":false,"error":{"code":"GATEWAY_UNAVAILABLE"}}', True
        failed = isinstance(result, GatewayFailure)
        if isinstance(result, GatewayFailure) and result.error.code in _REFUSAL_CODES:
            self.failed = True
        if (isinstance(result, GatewaySuccess)
                and self._tools[name] == GatewayTool.CONTRIBUTION_SUBMIT):
            self.submitted = True
        return json.dumps(result_document(result), separators=(",", ":")), failed
