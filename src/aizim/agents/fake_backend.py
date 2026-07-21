from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from aizim.domain import AgentRole, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.gateway import GatewayFailure, GatewaySuccess, GatewayTool

from .backend import AgentRequest, AgentResult, BackendIdentity


class _GatewayTransport(Protocol):
    role: AgentRole

    async def call(
        self, operation: GatewayTool | str, payload: dict[str, JsonValue]
    ) -> GatewaySuccess | GatewayFailure: ...

    async def aclose(self) -> None: ...


type GatewayConnector = Callable[[Path, str], Awaitable[_GatewayTransport]]


@dataclass(frozen=True, slots=True)
class FakeToolAction:
    operation: GatewayTool
    payload: dict[str, JsonValue]


class FakeAgentBackend:
    def __init__(
        self,
        actions: tuple[FakeToolAction, ...],
        *,
        connect: GatewayConnector,
    ) -> None:
        self._actions = actions
        self._connect = connect

    @property
    def identity(self) -> BackendIdentity:
        return BackendIdentity("fake", "deterministic-v1", None)

    async def run(self, request: AgentRequest) -> AgentResult:
        transport = await self._connect(request.gateway_broker_socket, request.gateway_session_id)
        documents: list[JsonValue] = []
        status = "submitted"
        try:
            if transport.role is not request.role:
                status = "failed"
            else:
                for action in self._actions:
                    result = await transport.call(action.operation, action.payload)
                    documents.append(_result_document(result))
                    if isinstance(result, GatewayFailure):
                        status = "failed"
                        break
        finally:
            await transport.aclose()
        summary = "fixture complete" if status == "submitted" else "gateway action failed"
        final = {"status": status, "summary": summary}
        return AgentResult(
            worker_id=request.worker_id,
            status=status,
            summary=summary,
            transport_event_hash=sha256_json(documents),
            final_message_hash=sha256_json(final),
            exit_code=0 if status == "submitted" else 4,
        )


def _result_document(result: GatewaySuccess | GatewayFailure) -> JsonValue:
    match result:
        case GatewaySuccess(result=value):
            return {"ok": True, "result": value}
        case GatewayFailure(error=error):
            return {
                "ok": False,
                "error": {"code": error.code, "message": error.message},
            }
