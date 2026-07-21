from __future__ import annotations

import asyncio  # noqa: ANYIO_OK -- the foundation contract requires asyncio Unix servers
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from aizim.domain.serialization import JsonValue, canonical_json

from .operations import (
    RpcErrorBody,
    RpcFailure,
    RpcProtocolError,
    RpcRequest,
    RpcResponse,
    RpcSuccess,
    rpc_failure,
)
from .service_ownership import (
    SocketIdentity,
    SocketOwnershipError,
    reclaim_stale_socket,
    record_owned_socket,
    remove_owned_socket,
)

MAX_FRAME_BYTES: Final = 1024 * 1024
_REQUEST_FIELDS: Final = frozenset({"operation", "params", "session_id"})


@dataclass(frozen=True, slots=True)
class SocketPathError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return self.reason


type Dispatch = Callable[[RpcRequest, bool], RpcResponse]


class RequestDispatcher(Protocol):
    def __call__(self, request: RpcRequest, trusted: bool) -> RpcResponse: ...


def _json_document(body: bytes) -> dict[str, JsonValue]:
    try:
        value: JsonValue = json.loads(body.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RpcProtocolError("MALFORMED_FRAME") from error
    if type(value) is not dict:
        raise RpcProtocolError("MALFORMED_FRAME")
    try:
        canonical_json(value)
    except (TypeError, ValueError) as error:
        raise RpcProtocolError("MALFORMED_FRAME") from error
    return value


def _text(document: dict[str, JsonValue], key: str) -> str:
    value = document.get(key)
    if type(value) is not str or not value:
        raise RpcProtocolError("MALFORMED_FRAME")
    return value


def _optional_text(document: dict[str, JsonValue], key: str) -> str | None:
    value = document.get(key)
    if value is None:
        return None
    if type(value) is not str or not value:
        raise RpcProtocolError("MALFORMED_FRAME")
    return value


def _decode_request(body: bytes) -> RpcRequest:
    document = _json_document(body)
    if document.keys() != _REQUEST_FIELDS:
        raise RpcProtocolError("MALFORMED_FRAME")
    params = document["params"]
    if type(params) is not dict:
        raise RpcProtocolError("MALFORMED_FRAME")
    return RpcRequest(
        operation=_text(document, "operation"),
        params=params,
        session_id=_optional_text(document, "session_id"),
    )


def _decode_response(body: bytes) -> RpcResponse:
    document = _json_document(body)
    ok = document.get("ok")
    if ok is True and document.keys() == {"ok", "result"}:
        return RpcSuccess(document["result"])
    error = document.get("error")
    if ok is not False or type(error) is not dict or document.keys() != {"ok", "error"}:
        raise RpcProtocolError("MALFORMED_FRAME")
    if error.keys() != {"code", "message", "event_id"}:
        raise RpcProtocolError("MALFORMED_FRAME")
    return RpcFailure(
        RpcErrorBody(
            code=_text(error, "code"),
            message=_text(error, "message"),
            event_id=_optional_text(error, "event_id"),
        )
    )


async def _write_response(writer: asyncio.StreamWriter, response: RpcResponse) -> None:
    encoded = canonical_json(response)
    if len(encoded) > MAX_FRAME_BYTES:
        encoded = canonical_json(
            rpc_failure("RESPONSE_TOO_LARGE", "response exceeds the service frame limit")
        )
    writer.write(len(encoded).to_bytes(4, "big") + encoded)
    await writer.drain()


class RpcServer:
    def __init__(self, socket_path: Path, service_session: str, dispatch: Dispatch) -> None:
        self._socket_path = socket_path
        self._service_session = service_session
        self._dispatch = dispatch
        self._server: asyncio.AbstractServer | None = None
        self._owned_socket: SocketIdentity | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self._closing = False

    def _accept_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writers.add(writer)
        handler = asyncio.create_task(self._handle_connection(reader, writer))
        self._handlers.add(handler)
        if self._closing:
            writer.close()

    async def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            reclaim_stale_socket(self._socket_path)
        except SocketOwnershipError as error:
            raise SocketPathError(error.reason) from error
        server = await asyncio.start_unix_server(
            self._accept_connection, path=str(self._socket_path)
        )
        owned_socket: SocketIdentity | None = None
        try:
            owned_socket = record_owned_socket(self._socket_path)
            self._socket_path.chmod(0o600)
        except (OSError, SocketOwnershipError):
            server.close()
            try:
                await server.wait_closed()
            finally:
                if owned_socket is not None:
                    remove_owned_socket(self._socket_path, owned_socket)
            raise
        self._closing = False
        self._server = server
        self._owned_socket = owned_socket

    async def _close_connections(self) -> BaseException | None:
        failure: BaseException | None = None
        while self._handlers:
            for writer in tuple(self._writers):
                writer.close()
            handlers = tuple(self._handlers)
            done, _ = await asyncio.wait(handlers)
            for handler in done:
                if not handler.cancelled():
                    error = handler.exception()
                    if failure is None and error is not None:
                        failure = error
        return failure

    async def close(self) -> None:
        server = self._server
        owned_socket = self._owned_socket
        failure: BaseException | None = None
        try:
            if server is not None:
                self._closing = True
                server.close()
                failure = await self._close_connections()
                await server.wait_closed()
                await asyncio.sleep(0)
                late_failure = await self._close_connections()
                if failure is None:
                    failure = late_failure
        finally:
            self._server = None
            self._owned_socket = None
            if owned_socket is not None:
                remove_owned_socket(self._socket_path, owned_socket)
        if failure is not None:
            raise failure

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        trusted = False
        try:
            while True:
                try:
                    header = await reader.readexactly(4)
                except asyncio.IncompleteReadError as error:
                    if error.partial:
                        await _write_response(
                            writer, rpc_failure("MALFORMED_FRAME", "frame header is incomplete")
                        )
                    return
                frame_size = int.from_bytes(header, "big")
                if frame_size > MAX_FRAME_BYTES:
                    await _write_response(
                        writer, rpc_failure("FRAME_TOO_LARGE", "request exceeds the frame limit")
                    )
                    return
                try:
                    body = await reader.readexactly(frame_size)
                    request = _decode_request(body)
                except asyncio.IncompleteReadError:
                    await _write_response(
                        writer, rpc_failure("MALFORMED_FRAME", "frame body is incomplete")
                    )
                    return
                except RpcProtocolError as error:
                    await _write_response(
                        writer, rpc_failure(error.code, "request frame is malformed")
                    )
                    return
                trusted = trusted or request.session_id == self._service_session
                await _write_response(writer, self._dispatch(request, trusted))
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            finally:
                self._writers.discard(writer)
                handler = asyncio.current_task()
                if handler is not None:
                    self._handlers.discard(handler)


async def start_rpc_server(
    socket_path: Path, service_session: str, dispatch: Dispatch
) -> RpcServer:
    server = RpcServer(socket_path, service_session, dispatch)
    await server.start()
    return server


async def rpc_call(socket_path: Path, request: RpcRequest) -> RpcResponse:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        encoded = canonical_json(request)
        if len(encoded) > MAX_FRAME_BYTES:
            raise RpcProtocolError("FRAME_TOO_LARGE")
        writer.write(len(encoded).to_bytes(4, "big") + encoded)
        await writer.drain()
        frame_size = int.from_bytes(await reader.readexactly(4), "big")
        if frame_size > MAX_FRAME_BYTES:
            raise RpcProtocolError("FRAME_TOO_LARGE")
        return _decode_response(await reader.readexactly(frame_size))
    finally:
        writer.close()
        await writer.wait_closed()
