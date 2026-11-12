from __future__ import annotations

import json

import pytest

from aizim.gateway.capabilities import GatewayError, GatewayFailure
from aizim.gateway.peer_identity import SessionDeniedError
from aizim.gateway.transport_frames import (
    GatewayTransportError,
    decode_call,
    decode_redemption,
    decode_result,
    decode_session_request,
    result_document,
)


def _hostile_json(attack: str) -> bytes:
    if attack == "deep":
        return b'{"value":' + b"[" * 1200 + b"0" + b"]" * 1200 + b"}"
    return b'{"value":' + b"9" * 5000 + b"}"


@pytest.mark.parametrize("attack", ("deep", "large_integer"))
def test_hostile_json_is_mapped_at_every_gateway_frame_boundary(attack: str) -> None:
    body = _hostile_json(attack)
    token = bytearray(b"fixture-token")

    assert decode_session_request(body) is None
    assert decode_call(len(token).to_bytes(2, "big") + token + body, token) is None
    with pytest.raises(SessionDeniedError):
        decode_redemption(body)
    with pytest.raises(GatewayTransportError):
        decode_result(body)


@pytest.mark.parametrize("delay", [0.001, 0.25, 60.0])
def test_gateway_retry_hint_survives_wire_roundtrip(delay: float) -> None:
    result = GatewayFailure(GatewayError("GATEWAY_RATE_LIMITED", "retry later", "event-1", delay))

    assert decode_result(json.dumps(result_document(result)).encode()) == result


@pytest.mark.parametrize("hint", [None, True, False, 0, -1, 60.001, "1", [], {},
                                  float("inf"), float("nan")])
def test_gateway_rejects_malformed_retry_hints(hint: object) -> None:
    document = {"ok": False, "error": {
        "code": "GATEWAY_RATE_LIMITED", "message": "retry later", "event_id": "event-1",
        "retry_after_seconds": hint,
    }}

    with pytest.raises(GatewayTransportError):
        decode_result(json.dumps(document).encode())


@pytest.mark.parametrize("code", ["CAPABILITY_DENIED", "TOOL_FAILED", "UNVERIFIED_DOCUMENT_APPLY"])
@pytest.mark.parametrize("hint", [None, 0.25])
def test_other_gateway_results_cannot_claim_retry_metadata(code: str, hint: float | None) -> None:
    document = {"ok": False, "error": {
        "code": code, "message": "failed", "event_id": "event-1", "retry_after_seconds": hint,
    }}

    with pytest.raises(GatewayTransportError):
        decode_result(json.dumps(document).encode())


def test_rate_limit_result_requires_a_retry_hint() -> None:
    with pytest.raises(GatewayTransportError):
        decode_result(b'{"ok":false,"error":{"code":"GATEWAY_RATE_LIMITED",'
                      b'"message":"retry later","event_id":null}}')


@pytest.mark.parametrize("field", ["code", "retry_after_seconds"])
def test_workers_cannot_add_reserved_result_fields_to_call_frames(field: str) -> None:
    token = bytearray(b"fixture-token")
    document = {"operation": "state.query", "payload": {}, field: "GATEWAY_RATE_LIMITED"}
    body = len(token).to_bytes(2, "big") + token + json.dumps(document).encode()

    assert decode_call(body, token) is None
