from __future__ import annotations

import asyncio
import os
import socket
import stat
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from aizim.domain import AgentRole
from aizim.gateway import (
    BrokerDependencies,
    BrokerLifecycleError,
    BrokerRegistration,
    DarwinPeerIdentityVerifier,
    GatewaySessionBroker,
    PeerIdentity,
    SessionDeniedError,
    current_process_image_sha256,
    redeem_session,
)

NOW = datetime(2026, 7, 21, 10, tzinfo=UTC)


@pytest.fixture
def socket_path() -> Iterator[Path]:
    with TemporaryDirectory(prefix="aizim-", dir="/tmp") as directory:
        yield Path(directory) / "gateway.sock"


class Ids:
    def __init__(self) -> None:
        self._next = 0

    def __call__(self) -> str:
        self._next += 1
        return f"session-{self._next}"


class StaticIdentity:
    def __init__(self, identity: PeerIdentity) -> None:
        self.identity = identity

    def __call__(self, peer_socket) -> PeerIdentity:
        del peer_socket
        return self.identity


class ControlledCloseBroker(GatewaySessionBroker):
    def __init__(self, path: Path, dependencies: BrokerDependencies) -> None:
        super().__init__(path, dependencies)
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()

    async def _cleanup(self) -> None:
        self.cleanup_started.set()
        await self.cleanup_release.wait()
        await super()._cleanup()


class ExplodingIdentity:
    def __call__(self, peer_socket) -> PeerIdentity:
        del peer_socket
        raise LookupError("injected verifier fault")


def registration(image_hash: str, *, token: str = "secret-token") -> BrokerRegistration:
    return BrokerRegistration(
        run_id="run-1",
        worker_id="worker-1",
        role=AgentRole.FORMALIZER,
        sidecar_executable_sha256=image_hash,
        expires_at=NOW + timedelta(minutes=5),
        raw_token=token,
    )


def fake_dependencies(image_hash: str) -> BrokerDependencies:
    return BrokerDependencies(
        clock=lambda: NOW,
        session_ids=Ids(),
        peer_identity=StaticIdentity(PeerIdentity(os.geteuid(), image_hash)),
    )


@pytest.mark.asyncio
async def test_real_darwin_identity_redeems_over_private_socket(socket_path: Path) -> None:
    if sys.platform != "darwin":
        pytest.skip("Darwin audit-token identity is macOS-only")
    image_hash = current_process_image_sha256()
    broker = GatewaySessionBroker(
        socket_path,
        dependencies=BrokerDependencies(
            clock=lambda: NOW,
            session_ids=Ids(),
            peer_identity=DarwinPeerIdentityVerifier(),
        ),
    )
    session_id = broker.register(registration(image_hash))

    await broker.start()
    try:
        redeemed = await redeem_session(broker.socket_path, session_id)
        mode = stat.S_IMODE(broker.socket_path.stat().st_mode)
    finally:
        await broker.aclose()

    assert mode == 0o600
    assert redeemed.raw_token == "secret-token"
    assert redeemed.run_id == "run-1"
    assert redeemed.worker_id == "worker-1"
    assert redeemed.role is AgentRole.FORMALIZER
    assert "secret-token" not in repr(redeemed)
    assert not broker.socket_path.exists()


@pytest.mark.asyncio
async def test_concurrent_redemption_has_exactly_one_winner(socket_path: Path) -> None:
    image_hash = "a" * 64
    broker = GatewaySessionBroker(
        socket_path, dependencies=fake_dependencies(image_hash)
    )
    session_id = broker.register(registration(image_hash))
    await broker.start()
    try:
        results = await asyncio.gather(
            redeem_session(broker.socket_path, session_id),
            redeem_session(broker.socket_path, session_id),
            return_exceptions=True,
        )
    finally:
        await broker.aclose()

    assert sum(not isinstance(result, BaseException) for result in results) == 1
    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], SessionDeniedError)


@pytest.mark.asyncio
async def test_live_socket_is_preserved_and_stale_socket_is_reclaimed(socket_path: Path) -> None:
    image_hash = "b" * 64
    path = socket_path
    first = GatewaySessionBroker(path, dependencies=fake_dependencies(image_hash))
    second = GatewaySessionBroker(path, dependencies=fake_dependencies(image_hash))
    second.register(registration(image_hash))
    secret = next(iter(second._registrations.values())).secret
    await first.start()
    try:
        with pytest.raises(BrokerLifecycleError):
            await second.start()
        assert path.is_socket()
        assert set(secret) == {0}
        assert second._registrations == {}
    finally:
        await first.aclose()

    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(path))
    stale.close()
    await second.start()
    assert path.is_socket()
    await second.aclose()


@pytest.mark.asyncio
async def test_pre_listener_start_failure_clears_registered_secret(socket_path: Path) -> None:
    occupied_parent = socket_path.parent / "occupied"
    occupied_parent.write_text("not a directory")
    broker = GatewaySessionBroker(
        occupied_parent / "gateway.sock", dependencies=fake_dependencies("b" * 64)
    )
    broker.register(registration("b" * 64))
    secret = next(iter(broker._registrations.values())).secret

    with pytest.raises(OSError):
        await broker.start()

    assert set(secret) == {0}
    assert broker._registrations == {}


@pytest.mark.asyncio
async def test_close_preserves_replacement_socket_inode(socket_path: Path) -> None:
    image_hash = "c" * 64
    path = socket_path
    broker = GatewaySessionBroker(path, dependencies=fake_dependencies(image_hash))
    await broker.start()
    path.unlink()
    replacement = socket.socket(socket.AF_UNIX)
    replacement.bind(str(path))
    replacement_inode = path.stat().st_ino
    try:
        await broker.aclose()
        assert path.stat().st_ino == replacement_inode
    finally:
        replacement.close()
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_close_drains_idle_client_and_allows_restart(socket_path: Path) -> None:
    image_hash = "d" * 64
    path = socket_path
    broker = GatewaySessionBroker(path, dependencies=fake_dependencies(image_hash))
    await broker.start()
    connections = await asyncio.gather(
        *(asyncio.open_unix_connection(str(path)) for _ in range(16))
    )
    await asyncio.wait_for(broker.aclose(), timeout=2)
    for _, writer in connections:
        writer.close()
        await writer.wait_closed()
    assert broker._handlers == set()
    assert broker._writers == set()

    restarted = GatewaySessionBroker(path, dependencies=fake_dependencies(image_hash))
    await restarted.start()
    await restarted.aclose()


@pytest.mark.asyncio
async def test_cancelled_close_finishes_cleanup_before_propagating(socket_path: Path) -> None:
    image_hash = "e" * 64
    broker = ControlledCloseBroker(
        socket_path, dependencies=fake_dependencies(image_hash)
    )
    await broker.start()
    close_task = asyncio.create_task(broker.aclose())
    await broker.cleanup_started.wait()
    close_task.cancel()
    await asyncio.sleep(0)
    close_task.cancel()
    broker.cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await close_task
    assert not broker.socket_path.exists()
    assert broker._handlers == set()
    assert broker._writers == set()
    await broker.aclose()


@pytest.mark.asyncio
async def test_peer_eof_drains_handler_without_loop_context(socket_path: Path) -> None:
    broker = GatewaySessionBroker(socket_path, dependencies=fake_dependencies("f" * 64))
    await broker.start()
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    del reader
    while not broker._handlers:
        await asyncio.sleep(0)
    writer.close()
    await writer.wait_closed()
    await asyncio.wait_for(broker._drained.wait(), timeout=2)
    assert broker._handlers == set()
    assert broker._writers == set()
    await broker.aclose()


@pytest.mark.asyncio
async def test_unexpected_handler_failure_is_retrieved_and_surfaced(socket_path: Path) -> None:
    dependencies = BrokerDependencies(
        clock=lambda: NOW, session_ids=Ids(), peer_identity=ExplodingIdentity()
    )
    broker = GatewaySessionBroker(socket_path, dependencies=dependencies)
    session_id = broker.register(registration("a" * 64))
    await broker.start()
    with pytest.raises(SessionDeniedError):
        await redeem_session(socket_path, session_id)
    with pytest.raises(LookupError, match="injected verifier fault"):
        await broker.aclose()
    assert not socket_path.exists()
