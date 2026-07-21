from __future__ import annotations

import asyncio
import os
import socket
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from hmac import compare_digest
from pathlib import Path
from typing import Final, cast

from aizim.domain import AgentRole
from aizim.state.service_ownership import (
    SocketIdentity,
    SocketOwnershipError,
    reclaim_stale_socket,
    record_owned_socket,
    remove_owned_socket,
)

from .peer_identity import (
    MAX_FRAME_BYTES,
    BrokerDependencies,
    BrokerLifecycleError,
    BrokerRegistration,
    PeerSocket,
    write_frame,
)
from .transport import (
    GatewayCaller,
    GatewayChannelClaims,
    GatewayChannelContext,
    serve_gateway_channel,
)
from .transport_frames import decode_session_request

_DENIAL: Final = {
    "ok": False,
    "error": {"code": "SESSION_DENIED", "message": "session is not available"},
}


@dataclass(slots=True)
class _PendingSession:
    run_id: str
    worker_id: str
    role: AgentRole
    image_hash: str
    expires_at: datetime
    lease_id: str | None
    secret: bytearray

    def channel(self, gateway: GatewayCaller) -> GatewayChannelContext:
        claims = GatewayChannelClaims(self.run_id, self.worker_id, self.role, self.lease_id)
        return GatewayChannelContext(gateway, claims, self.secret)

    def clear(self) -> None:
        self.secret[:] = b"\0" * len(self.secret)


class GatewaySessionBroker:
    def __init__(
        self,
        socket_path: Path,
        dependencies: BrokerDependencies | None = None,
        gateway: GatewayCaller | None = None,
    ) -> None:
        self.socket_path = socket_path
        self._dependencies = BrokerDependencies() if dependencies is None else dependencies
        self._gateway = gateway
        self._owner_euid = os.geteuid()
        self._registrations: dict[str, _PendingSession] = {}
        self._server: asyncio.AbstractServer | None = None
        self._owned_socket: SocketIdentity | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self._drained = asyncio.Event()
        self._drained.set()
        self._handler_failure: BaseException | None = None
        self._closing = False
        self._closed = False
        self._cleanup_task: asyncio.Task[None] | None = None

    def register(self, registration: BrokerRegistration) -> str:
        if self._closed:
            raise BrokerLifecycleError("session broker is closed")
        session_id = self._dependencies.session_ids()
        if type(session_id) is not str or not session_id or session_id in self._registrations:
            raise BrokerLifecycleError("session id is unavailable")
        self._registrations[session_id] = _PendingSession(
            registration.run_id,
            registration.worker_id,
            registration.role,
            registration.sidecar_executable_sha256,
            registration.expires_at,
            registration.lease_id,
            bytearray(registration.raw_token.encode()),
        )
        return session_id

    async def start(self) -> None:
        if self._closed or self._server is not None:
            raise BrokerLifecycleError("session broker cannot be started")
        listener: socket.socket | None = None
        owned: SocketIdentity | None = None
        started = False
        try:
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)
            reclaim_stale_socket(self.socket_path)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(self.socket_path))
            owned = record_owned_socket(self.socket_path)
            self.socket_path.chmod(0o600)
            listener.listen(socket.SOMAXCONN)
            listener.setblocking(False)
            server = await asyncio.start_unix_server(self._accept, sock=listener)
            self._owned_socket = owned
            self._server = server
            self._closing = False
            started = True
        except SocketOwnershipError as error:
            raise BrokerLifecycleError(error.reason) from error
        finally:
            if not started:
                if listener is not None:
                    listener.close()
                self._clear_registrations()
                if owned is not None:
                    remove_owned_socket(self.socket_path, owned)

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        handler = asyncio.create_task(self._handle(reader, writer))
        self._handlers.add(handler)
        self._drained.clear()
        handler.add_done_callback(self._handler_done)
        if self._closing:
            writer.close()

    def _handler_done(self, handler: asyncio.Task[None]) -> None:
        failure = None if handler.cancelled() else handler.exception()
        if failure is not None and self._handler_failure is None:
            self._handler_failure = failure.with_traceback(None)
        self._handlers.discard(handler)
        if not self._handlers:
            self._drained.set()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                header = await reader.readexactly(4)
            except asyncio.IncompleteReadError as error:
                if error.partial:
                    await write_frame(writer, _DENIAL)
                return
            size = int.from_bytes(header, "big")
            if size > MAX_FRAME_BYTES:
                await write_frame(writer, _DENIAL)
                return
            try:
                body = await reader.readexactly(size)
            except asyncio.IncompleteReadError:
                await write_frame(writer, _DENIAL)
                return
            request = decode_session_request(body)
            if request is None:
                await write_frame(writer, _DENIAL)
                return
            pending = self._registrations.pop(request[0], None)
            if pending is None:
                await write_frame(writer, _DENIAL)
                return
            response, channel = _DENIAL, False
            try:
                peer_socket = cast(PeerSocket | None, writer.get_extra_info("socket"))
                if peer_socket is not None:
                    identity = self._dependencies.peer_identity(peer_socket)
                    valid = (
                        identity.euid == self._owner_euid
                        and compare_digest(identity.executable_sha256, pending.image_hash)
                        and self._dependencies.clock() < pending.expires_at
                    )
                    if valid and (not request[1] or self._gateway is not None):
                        response = {
                            "ok": True,
                            "session": {
                                "raw_token": pending.secret.decode(),
                                "run_id": pending.run_id,
                                "worker_id": pending.worker_id,
                                "role": pending.role.value,
                            },
                        }
                        channel = request[1]
            except (OSError, RuntimeError, UnicodeError, ValueError):
                response = _DENIAL
            try:
                await write_frame(writer, response)
                if channel and self._gateway is not None:
                    await serve_gateway_channel(reader, writer, pending.channel(self._gateway))
            finally:
                pending.clear()
        except ConnectionError:
            return
        finally:
            writer.close()
            try:
                with suppress(ConnectionError):
                    await writer.wait_closed()
            finally:
                self._writers.discard(writer)

    def _clear_registrations(self) -> None:
        for pending in self._registrations.values():
            pending.clear()
        self._registrations.clear()

    async def _close_connections(self) -> None:
        while self._handlers:
            for writer in tuple(self._writers):
                writer.close()
            for handler in tuple(self._handlers):
                handler.cancel()
            await self._drained.wait()

    async def _cleanup(self) -> None:
        server, owned = self._server, self._owned_socket
        try:
            if server is not None:
                self._closing = True
                await asyncio.sleep(0)
                server.close()
                await self._close_connections()
                await server.wait_closed()
                await asyncio.sleep(0)
                await self._close_connections()
        finally:
            self._server = None
            self._owned_socket = None
            self._closed = True
            self._clear_registrations()
            if owned is not None:
                remove_owned_socket(self.socket_path, owned)
        failure = self._handler_failure
        self._handler_failure = None
        if failure is not None:
            raise failure

    async def aclose(self) -> None:
        task = self._cleanup_task
        if task is None:
            loop = asyncio.get_running_loop()
            task = asyncio.Task(self._cleanup(), loop=loop, eager_start=True)
            self._cleanup_task = task
        interruption: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError as error:
                if interruption is None:
                    interruption = error
        if interruption is not None:
            try:
                task.result()
            except BaseException as cleanup_error:
                raise cleanup_error from interruption
            raise interruption
        task.result()
