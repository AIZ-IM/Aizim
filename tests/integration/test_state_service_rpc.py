from __future__ import annotations

import asyncio  # noqa: ANYIO_OK -- exercises the asyncio Unix-server lifecycle
import socket
import stat
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from anyio.to_thread import run_sync

from aizim.domain.serialization import canonical_json
from aizim.state import StateDependencies, StateService, StateServiceConfig
from aizim.state import rpc as rpc_module
from aizim.state.rpc import (
    MAX_FRAME_BYTES,
    RpcFailure,
    RpcRequest,
    RpcServer,
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


def _ignore_control_commit(_operation: str) -> None:
    return


def _service(
    project_root: Path,
    session: str = "service-session",
    control_committed: Callable[[str], None] = _ignore_control_commit,
) -> StateService:
    config = StateServiceConfig(project_root=project_root, service_session=session)
    dependencies = StateDependencies(
        clock=lambda: datetime(2026, 7, 21, 10, tzinfo=UTC),
        event_ids=MonotoneIds(),
        control_committed=control_committed,
    )
    return StateService(config, dependencies)


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
async def test_completed_handler_failure_is_reported_after_socket_cleanup(
    project_root: Path,
) -> None:
    # Given
    socket_path = project_root / ".aizim" / "run" / "state.sock"
    handler_done, failure = asyncio.Event(), RuntimeError("injected dispatch failure")
    loop = asyncio.get_running_loop()
    previous_handler, unhandled_messages = loop.get_exception_handler(), list[str]()
    loop.set_exception_handler(lambda _, ctx: unhandled_messages.append(str(ctx.get("message"))))

    def fail_dispatch(_request: RpcRequest, _trusted: bool) -> RpcSuccess:
        handler = asyncio.current_task()
        assert handler is not None
        handler.add_done_callback(lambda _completed: handler_done.set())
        raise failure

    server = RpcServer(socket_path, "service-session", fail_dispatch)
    await server.start()
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    encoded = canonical_json(RpcRequest(operation="health", params={}, session_id=None))

    try:
        writer.write(len(encoded).to_bytes(4, "big") + encoded)
        await writer.drain()
        async with asyncio.timeout(0.25):
            assert await reader.read() == b""
            await handler_done.wait()

        # When / Then
        with pytest.raises(RuntimeError, match="injected dispatch failure") as raised:
            await server.close()
        assert raised.value is failure and not socket_path.exists()
        await server.close()

        restarted = RpcServer(socket_path, "service-session", fail_dispatch)
        await restarted.start()
        await restarted.close()
        assert "Task exception was never retrieved" not in unhandled_messages
    finally:
        writer.close()
        await writer.wait_closed()
        await server.close()
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_cancelled_close_finishes_cleanup_before_propagating(project_root: Path) -> None:
    # Given
    socket_path = project_root / ".aizim" / "run" / "state.sock"
    real_sleep, parked, release = asyncio.sleep, asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_handler, contexts = loop.get_exception_handler(), list[str]()
    loop.set_exception_handler(lambda _, ctx: contexts.append(str(ctx.get("message"))))
    server = RpcServer(socket_path, "service-session", lambda _request, _trusted: RpcSuccess({}))
    await server.start()
    raw_server = server._server
    assert raw_server is not None
    reader, writer = await asyncio.open_unix_connection(str(socket_path))

    async def block_preclose(_delay: float) -> None:
        parked.set()
        await release.wait()

    try:
        # When
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(rpc_module.asyncio, "sleep", block_preclose)
            close_task = asyncio.create_task(server.close())
            async with asyncio.timeout(0.25):
                while not parked.is_set() or not server._handlers:
                    await real_sleep(0)
            close_task.cancel()
            await real_sleep(0)
            close_task.cancel()
        release.set()

        # Then
        with pytest.raises(asyncio.CancelledError):
            await close_task
        assert not raw_server.is_serving() and not server._handlers and not server._writers
        assert not socket_path.exists() and await asyncio.wait_for(reader.read(), 0.25) == b""
        await server.close()
        assert not contexts and asyncio.all_tasks() == {asyncio.current_task()}
    finally:
        release.set()
        for peer in (*server._writers, writer, raw_server):
            peer.close()
        await asyncio.gather(raw_server.wait_closed(), writer.wait_closed())
        await asyncio.gather(*server._handlers, return_exceptions=True)
        loop.set_exception_handler(previous_handler)


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
async def test_public_control_mutations_do_not_grant_generic_append_authority(
    project_root: Path,
) -> None:
    # Given
    committed: list[str] = []
    async with _service(project_root, control_committed=committed.append) as service:
        requests = (
            RpcRequest(
                operation="control.configure_controller",
                params={"provider": "codex", "model": "gpt-5.6-sol"},
                session_id=None,
            ),
            RpcRequest(
                operation="control.register_worker",
                params={"worker_id": "worker-1", "role": "formalizer"},
                session_id=None,
            ),
            RpcRequest(
                operation="control.assign_task",
                params={"worker_id": "worker-1", "task": "prove the fixture"},
                session_id=None,
            ),
        )

        # When
        configured, registered, assigned = [
            await rpc_call(service.socket_path, request) for request in requests
        ]
        denied = await rpc_call(
            service.socket_path,
            RpcRequest(
                operation="append_event",
                params={
                    "event_type": "RunCreated",
                    "actor": "supervisor",
                    "run_id": "run-1",
                    "causation_id": None,
                    "payload": {},
                },
                session_id=None,
            ),
        )
        failed_control = await rpc_call(service.socket_path, requests[1])
        health = await rpc_call(
            service.socket_path,
            RpcRequest(operation="health", params={}, session_id=None),
        )

        # Then
        assert configured == RpcSuccess({"version": 1})
        assert registered == RpcSuccess({"version": 1})
        assert assigned == RpcSuccess({"version": 1})
        assert service.query_projection("controller", "primary") is not None
        assert isinstance(denied, RpcFailure)
        assert denied.error.code == "NOT_AUTHORIZED"
        assert isinstance(failed_control, RpcFailure)
        assert failed_control.error.code == "WORKER_ALREADY_REGISTERED"
        assert health == RpcSuccess({"event_schema_version": 1, "ready": True})
        assert committed == [request.operation for request in requests]


@pytest.mark.asyncio
async def test_committed_control_response_survives_wake_callback_failure(
    project_root: Path,
) -> None:
    # Given
    committed: list[str] = []

    def fail_wake(operation: str) -> None:
        committed.append(operation)
        raise RuntimeError("injected wake failure")

    async with _service(project_root, control_committed=fail_wake) as service:
        # When
        response = await rpc_call(
            service.socket_path,
            RpcRequest(
                operation="control.configure_controller",
                params={"provider": "codex", "model": None},
                session_id=None,
            ),
        )

        # Then
        assert response == RpcSuccess({"version": 1})
        assert service.query_projection("controller", "primary") is not None
        assert committed == ["control.configure_controller"]


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
