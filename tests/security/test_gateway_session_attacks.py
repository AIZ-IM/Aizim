from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from aizim.domain import AgentRole, canonical_json
from aizim.gateway import (
    BrokerDependencies,
    BrokerRegistration,
    GatewaySessionBroker,
    PeerIdentity,
    SessionDeniedError,
    redeem_session,
)

NOW = datetime(2026, 7, 21, 10, tzinfo=UTC)


@pytest.fixture
def socket_path() -> Iterator[Path]:
    with TemporaryDirectory(prefix="aizim-", dir="/tmp") as directory:
        yield Path(directory) / "gateway.sock"


class Identity:
    def __init__(self, euid: int, image_hash: str) -> None:
        self.euid = euid
        self.image_hash = image_hash

    def __call__(self, peer_socket) -> PeerIdentity:
        del peer_socket
        return PeerIdentity(self.euid, self.image_hash)


def registration(image_hash: str, *, expires_at: datetime | None = None) -> BrokerRegistration:
    return BrokerRegistration(
        run_id="run-1",
        worker_id="worker-1",
        role=AgentRole.FORMALIZER,
        sidecar_executable_sha256=image_hash,
        expires_at=NOW + timedelta(minutes=5) if expires_at is None else expires_at,
        raw_token="raw-secret-token",
    )


def broker(path: Path, identity: Identity) -> GatewaySessionBroker:
    return GatewaySessionBroker(
        path,
        dependencies=BrokerDependencies(
            clock=lambda: NOW,
            session_ids=lambda: "known-session",
            peer_identity=identity,
        ),
    )


async def raw_request(path: Path, body: bytes, *, declared_size: int | None = None) -> bytes:
    reader, writer = await asyncio.open_unix_connection(str(path))
    size = len(body) if declared_size is None else declared_size
    writer.write(size.to_bytes(4, "big") + body)
    await writer.drain()
    response_size = int.from_bytes(await reader.readexactly(4), "big")
    response = await reader.readexactly(response_size)
    writer.close()
    await writer.wait_closed()
    return response


def assert_generic_denial(response: bytes) -> None:
    assert json.loads(response) == {
        "ok": False,
        "error": {"code": "SESSION_DENIED", "message": "session is not available"},
    }
    assert b"raw-secret-token" not in response
    assert b"known-session" not in response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("peer_uid", "peer_hash"),
    [(os.geteuid() + 1, "a" * 64), (os.geteuid(), "b" * 64)],
)
async def test_wrong_uid_or_process_image_burns_registration_with_generic_error(
    socket_path: Path, peer_uid: int, peer_hash: str
) -> None:
    expected_hash = "a" * 64
    identity = Identity(peer_uid, peer_hash)
    gate = broker(socket_path, identity)
    session_id = gate.register(registration(expected_hash))
    await gate.start()
    try:
        with pytest.raises(SessionDeniedError) as first:
            await redeem_session(gate.socket_path, session_id)
        identity.euid = os.geteuid()
        identity.image_hash = expected_hash
        with pytest.raises(SessionDeniedError) as second:
            await redeem_session(gate.socket_path, session_id)
    finally:
        await gate.aclose()

    assert str(first.value) == "SESSION_DENIED"
    assert str(second.value) == "SESSION_DENIED"
    assert "raw-secret-token" not in repr(first.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("expires_at", [NOW - timedelta(seconds=1), NOW])
async def test_expired_registration_is_burned(
    socket_path: Path, expires_at: datetime
) -> None:
    image_hash = "c" * 64
    gate = broker(socket_path, Identity(os.geteuid(), image_hash))
    session_id = gate.register(registration(image_hash, expires_at=expires_at))
    await gate.start()
    try:
        with pytest.raises(SessionDeniedError):
            await redeem_session(gate.socket_path, session_id)
        with pytest.raises(SessionDeniedError):
            await redeem_session(gate.socket_path, session_id)
    finally:
        await gate.aclose()


@pytest.mark.asyncio
async def test_malformed_and_oversized_frames_are_generic_and_do_not_burn_other_ids(
    socket_path: Path,
) -> None:
    image_hash = "d" * 64
    gate = broker(socket_path, Identity(os.geteuid(), image_hash))
    valid_id = gate.register(registration(image_hash))
    await gate.start()
    try:
        malformed = await raw_request(gate.socket_path, b'{"wrong":"shape"}')
        oversized = await raw_request(gate.socket_path, b"", declared_size=4097)
        redeemed = await redeem_session(gate.socket_path, valid_id)
    finally:
        await gate.aclose()

    assert_generic_denial(malformed)
    assert_generic_denial(oversized)
    assert redeemed.raw_token == "raw-secret-token"


@pytest.mark.asyncio
async def test_guessed_session_id_does_not_consume_valid_registration(socket_path: Path) -> None:
    image_hash = "e" * 64
    gate = broker(socket_path, Identity(os.geteuid(), image_hash))
    valid_id = gate.register(registration(image_hash))
    await gate.start()
    try:
        guessed = await raw_request(
            gate.socket_path, canonical_json({"session_id": "guessed-session"})
        )
        redeemed = await redeem_session(gate.socket_path, valid_id)
    finally:
        await gate.aclose()

    assert_generic_denial(guessed)
    assert redeemed.raw_token == "raw-secret-token"


@pytest.mark.asyncio
async def test_registration_and_redeemed_reprs_never_contain_raw_token(
    socket_path: Path,
) -> None:
    image_hash = "f" * 64
    request = registration(image_hash)
    gate = broker(socket_path, Identity(os.geteuid(), image_hash))
    session_id = gate.register(request)
    await gate.start()
    try:
        redeemed = await redeem_session(gate.socket_path, session_id)
    finally:
        await gate.aclose()

    assert "raw-secret-token" not in repr(request)
    assert "raw-secret-token" not in repr(redeemed)
