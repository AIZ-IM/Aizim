from __future__ import annotations

import asyncio  # noqa: ANYIO_OK -- the foundation contract requires asyncio Unix servers
import json
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol

from aizim.domain.serialization import JsonValue, canonical_json

MAX_FRAME_BYTES: Final = 1024 * 1024
_REQUEST_FIELDS: Final = frozenset({"operation", "params", "session_id"})


@dataclass(frozen=True, slots=True)
class RpcProtocolError(ValueError):
    code: str

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class SocketPathError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class RpcRequest:
    operation: str
    params: dict[str, JsonValue]
    session_id: str | None


@dataclass(frozen=True, slots=True)
class RpcErrorBody:
    code: str
    message: str
    event_id: str | None


@dataclass(frozen=True, slots=True)
class RpcSuccess:
    ok: Literal[True]
    result: JsonValue

    def __init__(self, result: JsonValue) -> None:
        object.__setattr__(self, "ok", True)
        object.__setattr__(self, "result", result)


@dataclass(frozen=True, slots=True)
class RpcFailure:
    ok: Literal[False]
    error: RpcErrorBody

    def __init__(self, error: RpcErrorBody) -> None:
        object.__setattr__(self, "ok", False)
        object.__setattr__(self, "error", error)


type RpcResponse = RpcSuccess | RpcFailure
type Dispatch = Callable[[RpcRequest, bool], RpcResponse]


class RequestDispatcher(Protocol):
    def __call__(self, request: RpcRequest, trusted: bool) -> RpcResponse: ...


def rpc_failure(code: str, message: str, event_id: str | None = None) -> RpcFailure:
    return RpcFailure(RpcErrorBody(code=code, message=message, event_id=event_id))


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

    async def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            mode = self._socket_path.lstat().st_mode
        except FileNotFoundError:
            mode = None
        if mode is not None:
            if not stat.S_ISSOCK(mode):
                raise SocketPathError("state socket path is occupied by a non-socket entry")
            self._socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(self._socket_path)
        )
        self._socket_path.chmod(0o600)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            mode = self._socket_path.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(mode):
            self._socket_path.unlink()

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
            await writer.wait_closed()


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
