from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from hmac import compare_digest
from typing import Final

from aizim.domain import AgentRole, canonical_json
from aizim.domain.serialization import JsonValue

from .capabilities import GatewayError, GatewayFailure, GatewaySuccess, GatewayTool
from .peer_identity import SessionDeniedError

RPC_FRAME_LIMIT: Final = 1024 * 1024
TOKEN_LIMIT: Final = 4096
type GatewayResult = GatewaySuccess | GatewayFailure


@dataclass(frozen=True, slots=True)
class GatewayTransportError(RuntimeError):
    reason: str = "GATEWAY_UNAVAILABLE"

    def __str__(self) -> str:
        return self.reason


def decode_session_request(body: bytes) -> tuple[str, bool] | None:
    try:
        value: JsonValue = json.loads(body.decode())
    except (ValueError, RecursionError):
        return None
    if type(value) is not dict or value.keys() not in (
        {"session_id"},
        {"session_id", "channel"},
    ):
        return None
    session_id = value["session_id"]
    channel = value.get("channel", False)
    if type(session_id) is not str or not session_id or type(channel) is not bool:
        return None
    return session_id, channel


async def read_frame(reader: asyncio.StreamReader, *, clean_eof: bool) -> bytes | None:
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError as error:
        if clean_eof and not error.partial:
            return None
        raise
    size = int.from_bytes(header, "big")
    if size > RPC_FRAME_LIMIT:
        raise GatewayTransportError
    return await reader.readexactly(size)


def decode_redemption(body: bytes | None) -> tuple[AgentRole, bytearray]:
    try:
        if body is None:
            raise SessionDeniedError
        value: JsonValue = json.loads(body)
        if type(value) is not dict or value.get("ok") is not True:
            raise SessionDeniedError
        session = value.get("session")
        if type(session) is not dict or session.keys() != {
            "raw_token",
            "run_id",
            "worker_id",
            "role",
        }:
            raise SessionDeniedError
        raw_token = session.get("raw_token")
        role = session.get("role")
        if type(raw_token) is not str or not raw_token or type(role) is not str:
            raise SessionDeniedError
        return AgentRole(role), bytearray(raw_token.encode())
    except (TypeError, ValueError, RecursionError) as error:
        raise SessionDeniedError from error


def encode_call(
    operation: GatewayTool | str, payload: dict[str, JsonValue], token: bytearray
) -> bytes:
    token_bytes = bytes(token)
    if not token_bytes or len(token_bytes) > TOKEN_LIMIT:
        raise GatewayTransportError
    document = canonical_json({"operation": str(operation), "payload": payload})
    body = len(token_bytes).to_bytes(2, "big") + token_bytes + document
    if len(body) > RPC_FRAME_LIMIT:
        raise GatewayTransportError
    return body


def decode_call(body: bytes, secret: bytearray) -> tuple[str, dict[str, JsonValue], str] | None:
    if len(body) < 3:
        return None
    token_size = int.from_bytes(body[:2], "big")
    if token_size <= 0 or token_size > TOKEN_LIMIT or len(body) <= token_size + 2:
        return None
    token_bytes = body[2 : token_size + 2]
    if not compare_digest(token_bytes, bytes(secret)):
        return None
    try:
        value: JsonValue = json.loads(body[token_size + 2 :])
    except (ValueError, RecursionError):
        return None
    if type(value) is not dict or value.keys() != {"operation", "payload"}:
        return None
    operation, payload = value["operation"], value["payload"]
    if type(operation) is not str or not operation or type(payload) is not dict:
        return None
    return operation, payload, token_bytes.decode()


async def write_result(writer: asyncio.StreamWriter, result: GatewayResult) -> None:
    encoded = canonical_json(result_document(result))
    if len(encoded) > RPC_FRAME_LIMIT:
        encoded = canonical_json(
            result_document(
                GatewayFailure(
                    GatewayError("GATEWAY_RESPONSE_LIMIT", "gateway response is too large", None)
                )
            )
        )
    writer.write(len(encoded).to_bytes(4, "big") + encoded)
    await writer.drain()


def result_document(result: GatewayResult) -> dict[str, JsonValue]:
    match result:
        case GatewaySuccess(result=value):
            return {"ok": True, "result": value}
        case GatewayFailure(error=error):
            return {
                "ok": False,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "event_id": error.event_id,
                },
            }


def decode_result(body: bytes) -> GatewayResult:
    try:
        value: JsonValue = json.loads(body)
        if type(value) is not dict:
            raise GatewayTransportError
        if value.keys() == {"ok", "result"} and value["ok"] is True:
            return GatewaySuccess(value["result"])
        error = value.get("error")
        valid = value.keys() == {"ok", "error"} and value["ok"] is False and type(error) is dict
        if not valid or type(error) is not dict:
            raise GatewayTransportError
        code, message = error.get("code"), error.get("message")
        event_id = error.get("event_id")
        if (
            type(code) is not str
            or type(message) is not str
            or (event_id is not None and type(event_id) is not str)
        ):
            raise GatewayTransportError
        return GatewayFailure(GatewayError(code, message, event_id))
    except (ValueError, RecursionError) as error:
        raise GatewayTransportError from error


def protocol_failure() -> GatewayFailure:
    return GatewayFailure(GatewayError("GATEWAY_PROTOCOL", "gateway request is invalid", None))
