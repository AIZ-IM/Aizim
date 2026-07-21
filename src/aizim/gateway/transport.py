from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from aizim.async_lifecycle import await_cleanup
from aizim.domain import AgentRole
from aizim.domain.serialization import JsonValue

from .capabilities import (
    CapabilitySession,
    GatewayTool,
)
from .peer_identity import SessionDeniedError, write_frame
from .transport_frames import (
    GatewayResult,
    GatewayTransportError,
    decode_call,
    decode_redemption,
    decode_result,
    encode_call,
    protocol_failure,
    read_frame,
    write_result,
)


class GatewayCaller(Protocol):
    async def call(
        self,
        session: CapabilitySession,
        operation: GatewayTool | str,
        payload: dict[str, JsonValue],
    ) -> GatewayResult: ...


@dataclass(frozen=True, slots=True)
class GatewayChannelClaims:
    run_id: str
    worker_id: str
    role: AgentRole
    lease_id: str | None


@dataclass(frozen=True, slots=True)
class GatewayChannelContext:
    gateway: GatewayCaller = field(repr=False)
    claims: GatewayChannelClaims
    secret: bytearray = field(repr=False)


async def serve_gateway_channel(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    context: GatewayChannelContext,
) -> None:
    while True:
        try:
            body = await read_frame(reader, clean_eof=True)
        except GatewayTransportError:
            with suppress(ConnectionError):
                await write_result(writer, protocol_failure())
            return
        except asyncio.IncompleteReadError:
            return
        if body is None:
            return
        request = decode_call(body, context.secret)
        if request is None:
            await write_result(writer, protocol_failure())
            continue
        operation, payload, token = request
        try:
            result = await context.gateway.call(
                CapabilitySession(
                    token,
                    context.claims.run_id,
                    context.claims.worker_id,
                    context.claims.role.value,
                    context.claims.lease_id,
                ),
                operation,
                payload,
            )
        finally:
            token = ""
        await write_result(writer, result)


class GatewayTransport:
    __slots__ = (
        "_close_task",
        "_closed",
        "_disconnected",
        "_error",
        "_lock",
        "_reader",
        "_reader_task",
        "_responses",
        "_token",
        "_writer",
        "role",
    )

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        role: AgentRole,
        token: bytearray,
    ) -> None:
        self.role = role
        self._reader = reader
        self._writer = writer
        self._token = token
        self._lock = asyncio.Lock()
        self._responses: asyncio.Queue[GatewayResult | None] = asyncio.Queue(maxsize=1)
        self._disconnected = asyncio.Event()
        self._error: GatewayTransportError | None = None
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._reader_task = asyncio.create_task(self._read_responses())

    def __repr__(self) -> str:
        return "GatewayTransport(token=<redacted>, claims=<redacted>)"

    async def call(
        self, operation: GatewayTool | str, payload: dict[str, JsonValue]
    ) -> GatewayResult:
        async with self._lock:
            if self._closed or self._error is not None:
                raise GatewayTransportError
            body = encode_call(operation, payload, self._token)
            try:
                self._writer.write(len(body).to_bytes(4, "big") + body)
                await self._writer.drain()
                response = await self._responses.get()
                if response is None:
                    raise GatewayTransportError
                return response
            except ConnectionError as error:
                self._start_closing()
                raise GatewayTransportError from error
            except BaseException:
                self._start_closing()
                raise

    async def wait_disconnected(self) -> None:
        await self._disconnected.wait()

    async def aclose(self) -> None:
        self._start_closing()
        task = self._close_task
        assert task is not None
        interruption = await await_cleanup(task)
        if interruption is not None:
            raise interruption

    async def _read_responses(self) -> None:
        try:
            while True:
                body = await read_frame(self._reader, clean_eof=False)
                if body is None:
                    raise GatewayTransportError
                await self._responses.put(decode_result(body))
        except (ConnectionError, GatewayTransportError, asyncio.IncompleteReadError):
            self._start_closing()

    def _start_closing(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._token[:] = b"\0" * len(self._token)
            self._writer.close()
            if self._reader_task is not asyncio.current_task():
                self._reader_task.cancel()
            self._close_task = asyncio.create_task(self._finish_close())
        self._error = GatewayTransportError()
        self._disconnected.set()
        if self._responses.empty():
            self._responses.put_nowait(None)

    async def _finish_close(self) -> None:
        with suppress(asyncio.CancelledError):
            await self._reader_task
        with suppress(ConnectionError):
            await self._writer.wait_closed()


async def connect_gateway(socket_path: Path, session_id: str) -> GatewayTransport:
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
    except OSError as error:
        raise SessionDeniedError from error
    connected = False
    try:
        await write_frame(writer, {"session_id": session_id, "channel": True})
        body = await read_frame(reader, clean_eof=False)
        role, token = decode_redemption(body)
        transport = GatewayTransport(reader, writer, role, token)
        connected = True
        return transport
    except (ConnectionError, GatewayTransportError, asyncio.IncompleteReadError) as error:
        raise SessionDeniedError from error
    finally:
        if not connected:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
