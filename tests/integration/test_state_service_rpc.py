from __future__ import annotations

import socket
import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from anyio.to_thread import run_sync

from aizim.state import StateDependencies, StateService, StateServiceConfig
from aizim.state.rpc import (
    MAX_FRAME_BYTES,
    RpcFailure,
    RpcRequest,
    RpcSuccess,
    rpc_call,
)


class MonotoneIds:
    def __init__(self) -> None:
        self._next = 0

    def __call__(self) -> str:
        value = f"{self._next:023d}"
        self._next += 1
        return f"01J{value}"


@pytest.fixture
def project_root() -> Iterator[Path]:
    with TemporaryDirectory(prefix="aizim-", dir="/tmp") as directory:
        yield Path(directory)


def _service(project_root: Path, session: str = "service-session") -> StateService:
    return StateService(
        StateServiceConfig(project_root=project_root, service_session=session),
        StateDependencies(
            clock=lambda: datetime(2026, 7, 21, 10, tzinfo=UTC),
            event_ids=MonotoneIds(),
        ),
    )


def _receive_exact(client: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = client.recv(remaining)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _raw_exchange(socket_path: Path, declared_size: int, body: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(socket_path))
        client.sendall(declared_size.to_bytes(4, "big"))
        if body:
            client.sendall(body)
        response_size = int.from_bytes(_receive_exact(client, 4), "big")
        return _receive_exact(client, response_size)


@pytest.mark.asyncio
async def test_socket_is_private_and_health_hides_storage_details(project_root: Path) -> None:
    # Given
    async with _service(project_root) as service:
        socket_path = service.socket_path

        # When
        response = await rpc_call(
            socket_path,
            RpcRequest(operation="health", params={}, session_id=None),
        )

        # Then
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        assert isinstance(response, RpcSuccess)
        assert response.result == {
            "event_schema_version": 1,
            "ready": True,
        }
        encoded = repr(response)
        assert str(project_root) not in encoded
        assert "SELECT" not in encoded
        assert "sqlite" not in encoded.lower()


@pytest.mark.asyncio
async def test_append_requires_socket_bound_service_session(project_root: Path) -> None:
    # Given
    async with _service(project_root) as service:
        request = RpcRequest(
            operation="append_event",
            session_id=None,
            params={
                "event_type": "RunCreated",
                "actor": "supervisor",
                "run_id": "run-1",
                "causation_id": None,
                "payload": {},
            },
        )

        # When
        denied = await rpc_call(service.socket_path, request)
        accepted = await rpc_call(
            service.socket_path,
            RpcRequest(
                operation=request.operation,
                params=request.params,
                session_id="service-session",
            ),
        )

        # Then
        assert isinstance(denied, RpcFailure)
        assert denied.error.code == "NOT_AUTHORIZED"
        assert isinstance(accepted, RpcSuccess)
        assert accepted.result == {
            "event_id": "01J00000000000000000000000",
            "sequence": 1,
        }


@pytest.mark.asyncio
async def test_malformed_and_oversized_frames_fail_without_sensitive_output(
    project_root: Path,
) -> None:
    # Given
    async with _service(project_root) as service:
        # When
        malformed = await run_sync(_raw_exchange, service.socket_path, 1, b"{")
        exact_limit = await run_sync(
            _raw_exchange,
            service.socket_path,
            MAX_FRAME_BYTES,
            b"x" * MAX_FRAME_BYTES,
        )
        oversized = await run_sync(
            _raw_exchange,
            service.socket_path,
            MAX_FRAME_BYTES + 1,
            b"",
        )

    # Then
    assert b'"code":"MALFORMED_FRAME"' in malformed
    assert b'"code":"MALFORMED_FRAME"' in exact_limit
    assert b'"code":"FRAME_TOO_LARGE"' in oversized
    combined = malformed + exact_limit + oversized
    assert str(project_root).encode() not in combined
    assert b"SELECT" not in combined


@pytest.mark.asyncio
async def test_restart_preserves_rpc_projection_and_digest(project_root: Path) -> None:
    # Given
    async with _service(project_root) as service:
        appended = await rpc_call(
            service.socket_path,
            RpcRequest(
                operation="append_event",
                session_id="service-session",
                params={
                    "event_type": "RunCreated",
                    "actor": "supervisor",
                    "run_id": "run-1",
                    "causation_id": None,
                    "payload": {},
                },
            ),
        )
        before = await rpc_call(
            service.socket_path,
            RpcRequest(operation="logical_digest", params={}, session_id=None),
        )
        assert isinstance(appended, RpcSuccess)

    # When
    async with _service(project_root) as restarted:
        projection = await rpc_call(
            restarted.socket_path,
            RpcRequest(
                operation="query_projection",
                params={"projection_name": "runs", "entity_id": "run-1"},
                session_id=None,
            ),
        )
        after = await rpc_call(
            restarted.socket_path,
            RpcRequest(operation="logical_digest", params={}, session_id=None),
        )
        replay = await rpc_call(
            restarted.socket_path,
            RpcRequest(operation="replay_verify", params={}, session_id=None),
        )
        expected_digest = restarted.logical_digest()

    # Then
    assert isinstance(projection, RpcSuccess)
    assert projection.result is not None
    assert before == after
    assert isinstance(replay, RpcSuccess)
    assert replay.result == {"matched": True, "logical_digest": expected_digest}
