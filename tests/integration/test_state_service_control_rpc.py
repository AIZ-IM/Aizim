from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from aizim.state import StateDependencies, StateService, StateServiceConfig
from aizim.state.rpc import RpcFailure, RpcRequest, RpcSuccess, rpc_call


class MonotoneIds:
    def __init__(self) -> None:
        self._next = 0

    def __call__(self) -> str:
        value = f"{self._next:023d}"
        self._next += 1
        return f"01J{value}"


class InjectedWakeFailure(RuntimeError):
    pass


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
        raise InjectedWakeFailure

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
