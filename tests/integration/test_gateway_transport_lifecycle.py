from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import anyio
import pytest

from aizim.domain import AgentRole, canonical_json
from aizim.domain.serialization import JsonValue
from aizim.gateway import (
    BrokerDependencies,
    BrokerRegistration,
    CapabilitySession,
    GatewaySessionBroker,
    GatewaySuccess,
    GatewayTool,
    PeerIdentity,
)
from aizim.gateway.sidecar import serve_connected_sidecar
from aizim.gateway.transport import (
    GatewayTransport,
    GatewayTransportError,
    connect_gateway,
)


def _socket_path(tmp_path: Path) -> Path:
    suffix = sha256(str(tmp_path).encode()).hexdigest()[:12]
    return Path("/tmp") / f"aizim-{os.getpid()}-{suffix}.sock"


async def _read_call(reader: asyncio.StreamReader) -> None:
    size = int.from_bytes(await reader.readexactly(4), "big")
    await reader.readexactly(size)


async def _write_success(writer: asyncio.StreamWriter, sequence: int) -> None:
    body = canonical_json({"ok": True, "result": {"sequence": sequence}})
    writer.write(len(body).to_bytes(4, "big") + body)
    await writer.drain()


async def test_transport_close_unblocks_an_inflight_call(tmp_path: Path) -> None:
    request_seen, release = asyncio.Event(), asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await _read_call(reader)
            request_seen.set()
            await release.wait()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    socket_path = _socket_path(tmp_path)
    server = await asyncio.start_unix_server(handle, path=socket_path)
    reader, writer = await asyncio.open_unix_connection(socket_path)
    token = bytearray(b"fixture-token")
    transport = GatewayTransport(reader, writer, AgentRole.FORMALIZER, token)
    call = asyncio.create_task(transport.call("lean.goal", {}))
    try:
        await asyncio.wait_for(request_seen.wait(), 1)
        await asyncio.wait_for(transport.aclose(), 1)
        with pytest.raises(GatewayTransportError):
            await asyncio.wait_for(call, 1)
        assert set(token) == {0}
    finally:
        release.set()
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        server.close()
        await server.wait_closed()


async def test_cancelled_call_poisoning_prevents_response_misattribution(
    tmp_path: Path,
) -> None:
    request_seen, release = asyncio.Event(), asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await _read_call(reader)
            request_seen.set()
            await release.wait()
            await _write_success(writer, 1)
            await _read_call(reader)
            await _write_success(writer, 2)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    socket_path = _socket_path(tmp_path)
    server = await asyncio.start_unix_server(handle, path=socket_path)
    reader, writer = await asyncio.open_unix_connection(socket_path)
    transport = GatewayTransport(reader, writer, AgentRole.FORMALIZER, bytearray(b"fixture-token"))
    first = asyncio.create_task(transport.call("lean.goal", {}))
    try:
        await asyncio.wait_for(request_seen.wait(), 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        with pytest.raises(GatewayTransportError):
            await asyncio.wait_for(transport.call("lean.context", {}), 1)
    finally:
        await transport.aclose()
        server.close()
        await server.wait_closed()


async def test_invalid_gateway_response_fails_call_and_close_without_hanging(
    tmp_path: Path,
) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await _read_call(reader)
            body = b'{"ok":true,"result":' + b"9" * 5000 + b"}"
            writer.write(len(body).to_bytes(4, "big") + body)
            await writer.drain()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    socket_path = _socket_path(tmp_path)
    server = await asyncio.start_unix_server(handle, path=socket_path)
    reader, writer = await asyncio.open_unix_connection(socket_path)
    transport = GatewayTransport(reader, writer, AgentRole.FORMALIZER, bytearray(b"fixture-token"))
    try:
        with pytest.raises(GatewayTransportError):
            await asyncio.wait_for(transport.call("lean.goal", {}), 1)
        await transport.aclose()
    finally:
        with suppress(Exception):
            await transport.aclose()
        server.close()
        await server.wait_closed()


class _WaitingChannel:
    role = AgentRole.FORMALIZER

    def __init__(self) -> None:
        self.disconnected = asyncio.Event()
        self.closed = False

    async def call(self, operation: str, payload: dict[str, JsonValue]) -> GatewaySuccess:
        del operation, payload
        return GatewaySuccess({})

    async def wait_disconnected(self) -> None:
        await self.disconnected.wait()

    async def aclose(self) -> None:
        self.closed = True


async def test_cancelling_sidecar_reaps_all_owned_tasks() -> None:
    client_send, server_receive = anyio.create_memory_object_stream(1)
    server_send, client_receive = anyio.create_memory_object_stream(1)
    channel = _WaitingChannel()
    baseline = asyncio.all_tasks()
    sidecar = asyncio.create_task(serve_connected_sidecar(channel, server_receive, server_send))
    await asyncio.sleep(0)
    owned = asyncio.all_tasks() - baseline - {sidecar}
    assert owned
    sidecar.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await sidecar
        await asyncio.sleep(0)
        assert channel.closed
        assert all(task.done() for task in owned)
    finally:
        for task in owned:
            task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)
        await client_send.aclose()
        await client_receive.aclose()


class _BlockingGateway:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def call(
        self,
        session: CapabilitySession,
        operation: GatewayTool | str,
        payload: dict[str, JsonValue],
    ) -> GatewaySuccess:
        del session, operation, payload
        self.started.set()
        try:
            await self.release.wait()
        finally:
            self.cancelled.set()
        return GatewaySuccess({})


async def test_broker_close_cancels_a_blocked_gateway_call(tmp_path: Path) -> None:
    now = datetime(2026, 7, 21, tzinfo=UTC)
    image_hash = "3" * 64
    gateway = _BlockingGateway()
    broker = GatewaySessionBroker(
        _socket_path(tmp_path),
        BrokerDependencies(
            clock=lambda: now,
            session_ids=lambda: "session-7",
            peer_identity=lambda _socket: PeerIdentity(os.geteuid(), image_hash),
        ),
        gateway=gateway,
    )
    session_id = broker.register(
        BrokerRegistration(
            "run-7",
            "worker-7",
            AgentRole.FORMALIZER,
            image_hash,
            now + timedelta(minutes=5),
            "fixture-token",
        )
    )
    await broker.start()
    transport = await connect_gateway(broker.socket_path, session_id)
    call = asyncio.create_task(transport.call("lean.goal", {}))
    await gateway.started.wait()
    close = asyncio.create_task(broker.aclose())
    try:
        await asyncio.wait_for(gateway.cancelled.wait(), 0.2)
        await close
        with pytest.raises(GatewayTransportError):
            await call
    finally:
        gateway.release.set()
        await asyncio.gather(close, call, return_exceptions=True)
        await transport.aclose()
