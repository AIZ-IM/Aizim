from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from aizim.cli import main as cli_main
from aizim.domain import AgentRole
from aizim.gateway import (
    AuthorizedCall,
    BrokerDependencies,
    BrokerRegistration,
    CapabilityDependencies,
    CapabilityGateway,
    CapabilityGrant,
    CapabilityIssuer,
    GatewayLimits,
    GatewaySessionBroker,
    GatewayTool,
    PeerIdentity,
)
from aizim.gateway.mcp_tools import create_gateway_server
from aizim.gateway.peer_identity import write_frame
from aizim.gateway.sidecar import SidecarDisconnectedError, serve_connected_sidecar
from aizim.gateway.transport import connect_gateway
from aizim.gateway.transport_frames import RPC_FRAME_LIMIT
from aizim.state import StateDependencies, StateService, StateServiceConfig

NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)
IMAGE_HASH = "1" * 64
RAW_TOKEN = "A" * 43


class Ids:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._next = 0

    def __call__(self) -> str:
        self._next += 1
        return f"{self._next:026d}"


def state_service(project: Path) -> StateService:
    return StateService(
        StateServiceConfig(project, "task-7-state"),
        StateDependencies(clock=lambda: NOW, event_ids=Ids("event")),
    )


def broker_dependencies() -> BrokerDependencies:
    return BrokerDependencies(
        clock=lambda: NOW,
        session_ids=Ids("session"),
        peer_identity=lambda _socket: PeerIdentity(os.geteuid(), IMAGE_HASH),
    )


def registration(
    role: AgentRole,
    raw_token: str,
    operations: tuple[GatewayTool, ...] | None = None,
) -> BrokerRegistration:
    return BrokerRegistration(
        run_id="run-7",
        worker_id="worker-7",
        role=role,
        sidecar_executable_sha256=IMAGE_HASH,
        expires_at=NOW + timedelta(minutes=5),
        raw_token=raw_token,
        operations=operations,
    )


def broker_socket(tmp_path: Path) -> Path:
    suffix = sha256(str(tmp_path).encode()).hexdigest()[:12]
    return Path("/tmp") / f"aizim-{suffix}.sock"


def gateway(state: StateService, calls: list[AuthorizedCall]) -> CapabilityGateway:
    def query(call: AuthorizedCall):
        calls.append(call)
        return {"worker_id": call.worker_id, "payload": call.payload}

    return CapabilityGateway(
        state,
        {GatewayTool.STATE_QUERY: query},
        GatewayLimits(100, 60.0, 100),
        CapabilityDependencies(
            clock=lambda: NOW,
            monotonic=lambda: 10.0,
            request_ids=Ids("request"),
        ),
    )


async def test_sidecar_lists_role_tools_and_forwards_public_results(tmp_path: Path) -> None:
    calls: list[AuthorizedCall] = []
    role = AgentRole.RESEARCH_CONDUCTOR
    socket_path = broker_socket(tmp_path)
    with state_service(tmp_path) as state:
        raw_token = CapabilityIssuer(state, token_factory=lambda: RAW_TOKEN).mint(
            CapabilityGrant(
                run_id="run-7",
                worker_id="worker-7",
                role=role,
                lease_id=None,
                operations=(GatewayTool.STATE_QUERY,),
                expires_at=NOW + timedelta(minutes=5),
            )
        )
        broker = GatewaySessionBroker(
            socket_path, broker_dependencies(), gateway=gateway(state, calls)
        )
        session_id = broker.register(registration(role, raw_token, (GatewayTool.STATE_QUERY,)))
        await broker.start()
        try:
            transport = await connect_gateway(socket_path, session_id)
            server = create_gateway_server(transport)
            async with create_connected_server_and_client_session(server) as client:
                listed = await client.list_tools()
                result = await client.call_tool(
                    GatewayTool.STATE_QUERY.value,
                    {"payload": {"query": "workers"}},
                )
        finally:
            await transport.aclose()
            await broker.aclose()

    assert tuple(tool.name for tool in listed.tools) == (GatewayTool.STATE_QUERY.value,)
    assert listed.tools[0].inputSchema["properties"]["payload"] == {
        "type": "object",
        "properties": {"query": {"type": "string", "minLength": 1}},
        "required": ["query"],
        "additionalProperties": False,
    }
    assert result.isError is False
    assert result.structuredContent == {
        "ok": True,
        "result": {"worker_id": "worker-7", "payload": {"query": "workers"}},
    }
    assert len(calls) == 1
    assert calls[0].role is role


async def test_forged_undiscovered_tool_reaches_call_time_denial(tmp_path: Path) -> None:
    calls: list[AuthorizedCall] = []
    role = AgentRole.RESEARCH_CONDUCTOR
    socket_path = broker_socket(tmp_path)
    with state_service(tmp_path) as state:
        raw_token = CapabilityIssuer(state, token_factory=lambda: RAW_TOKEN).mint(
            CapabilityGrant(
                "run-7",
                "worker-7",
                role,
                None,
                (GatewayTool.STATE_QUERY,),
                NOW + timedelta(minutes=5),
            )
        )
        broker = GatewaySessionBroker(
            socket_path, broker_dependencies(), gateway=gateway(state, calls)
        )
        session_id = broker.register(registration(role, raw_token))
        await broker.start()
        try:
            transport = await connect_gateway(socket_path, session_id)
            async with create_connected_server_and_client_session(
                create_gateway_server(transport)
            ) as client:
                result = await client.call_tool(
                    GatewayTool.LEAN_BUILD.value,
                    {"payload": {"clean": False, "fetch_cache": False}},
                )
        finally:
            await transport.aclose()
            await broker.aclose()
        denials = tuple(
            event
            for event in state.query_events("run-7")
            if event.envelope.event_type == "CapabilityDenied"
        )

    assert result.isError is True
    assert result.structuredContent is not None
    assert result.structuredContent["error"]["code"] == "CAPABILITY_DENIED"
    assert calls == []
    assert len(denials) == 1
    rendered = json.dumps(result.structuredContent)
    assert RAW_TOKEN not in rendered
    assert str(socket_path) not in rendered


class DisconnectingChannel:
    role = AgentRole.FORMALIZER

    def __init__(self) -> None:
        self.disconnected = asyncio.Event()
        self.closed = False

    async def call(self, operation, payload):
        del operation, payload
        raise AssertionError("no tool call expected")

    async def wait_disconnected(self) -> None:
        await self.disconnected.wait()

    async def aclose(self) -> None:
        self.closed = True


async def test_sidecar_server_terminates_when_gateway_disconnects() -> None:
    client_send, server_receive = anyio.create_memory_object_stream(1)
    server_send, client_receive = anyio.create_memory_object_stream(1)
    channel = DisconnectingChannel()
    task = asyncio.create_task(serve_connected_sidecar(channel, server_receive, server_send))

    channel.disconnected.set()

    with pytest.raises(SidecarDisconnectedError):
        await task
    assert channel.closed
    await client_send.aclose()
    await client_receive.aclose()


def test_hidden_gateway_sidecar_cli_route(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[Path, str]] = []

    def run_sidecar(socket_path: Path, session_id: str) -> int:
        captured.append((socket_path, session_id))
        return 6

    monkeypatch.setattr(cli_main, "run_gateway_sidecar", run_sidecar)

    result = cli_main.main(
        [
            "gateway",
            "sidecar",
            "--broker-socket",
            "/tmp/aizim-gateway.sock",
            "--session-id",
            "session-7",
        ]
    )

    assert result == 6
    assert captured == [(Path("/tmp/aizim-gateway.sock"), "session-7")]


@pytest.mark.parametrize("attack", ("oversized", "partial"))
async def test_malformed_channel_frame_does_not_fail_broker_close(
    tmp_path: Path, attack: str
) -> None:
    socket_path = broker_socket(tmp_path)
    with state_service(tmp_path) as state:
        broker = GatewaySessionBroker(
            socket_path, broker_dependencies(), gateway=gateway(state, [])
        )
        session_id = broker.register(registration(AgentRole.RESEARCH_CONDUCTOR, RAW_TOKEN))
        await broker.start()
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        await write_frame(writer, {"session_id": session_id, "channel": True})
        size = int.from_bytes(await reader.readexactly(4), "big")
        await reader.readexactly(size)
        declared = RPC_FRAME_LIMIT + 1 if attack == "oversized" else 8
        writer.write(declared.to_bytes(4, "big"))
        if attack == "partial":
            writer.write(b"x")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0)
        await broker.aclose()
