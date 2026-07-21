from __future__ import annotations

import pytest

from aizim.gateway.peer_identity import SessionDeniedError
from aizim.gateway.transport_frames import (
    GatewayTransportError,
    decode_call,
    decode_redemption,
    decode_result,
    decode_session_request,
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
