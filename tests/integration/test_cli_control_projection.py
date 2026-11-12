from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from aizim.cli.control_projection import controller_document, worker_document
from aizim.cli.state_client import ProjectionDocument, StateClientError, load_projections
from aizim.domain.serialization import JsonValue
from aizim.runtime.layout import ProjectLayout
from aizim.state import AppendEventCommand, StateService, StateServiceConfig

ASSIGNMENT_ID = "a" * 64
SESSION_ID = f"controller-{'1' * 32}"
EXECUTION_ID = f"execution-{'2' * 32}"


def projection(
    name: str,
    entity_id: str,
    payload: dict[str, JsonValue],
    version: int = 1,
) -> ProjectionDocument:
    return ProjectionDocument(entity_id, name, {"payload": payload}, version)


def payload_of(record: ProjectionDocument) -> dict[str, JsonValue]:
    payload = record.state["payload"]
    assert type(payload) is dict
    return payload


def controller() -> ProjectionDocument:
    return projection(
        "controller",
        "primary",
        {"controller_id": "primary", "provider": "codex", "model": "gpt-5.6-sol"},
    )


def runtime(status: str, reason_code: str | None = None) -> ProjectionDocument:
    payload: dict[str, JsonValue] = {
        "controller_id": "primary",
        "controller_session_id": SESSION_ID,
        "controller_version": 1,
        "provider": "codex",
        "backend_version": "codex-cli 0.154.0",
        "executable_hash": "b" * 64,
        "status": status,
    }
    if reason_code is not None:
        payload["reason_code"] = reason_code
    return projection("controller_runtime", "primary", payload)


def roster() -> ProjectionDocument:
    return projection(
        "worker_roster",
        "proof-a",
        {"worker_id": "proof-a", "role": "proof_explorer", "status": "idle"},
    )


def assignment() -> ProjectionDocument:
    return projection(
        "worker_assignments",
        "proof-a",
        {
            "assignment_id": ASSIGNMENT_ID,
            "controller_id": "primary",
            "worker_id": "proof-a",
            "task": "prove the target",
            "task_hash": "c" * 64,
            "task_version": 1,
        },
    )


def execution(status: str, reason_code: str | None = None) -> ProjectionDocument:
    payload: dict[str, JsonValue] = {
        "assignment_id": ASSIGNMENT_ID,
        "controller_id": "primary",
        "controller_session_id": SESSION_ID,
        "controller_version": 1,
        "worker_id": "proof-a",
        "task_version": 1,
        "execution_id": EXECUTION_ID,
        "status": status,
    }
    if status in {"planned", "completed"}:
        payload["directive_id"] = f"directive-{'3' * 32}"
        payload["directive_artifact_hash"] = "d" * 64
        payload["instruction_hash"] = "e" * 64
        payload["budget"] = 3
        payload["timeout_milliseconds"] = 15_000
    if status == "completed":
        payload["result_hash"] = "f" * 64
    if reason_code is not None:
        payload["reason_code"] = reason_code
    return projection("worker_executions", ASSIGNMENT_ID, payload)


def test_controller_document_adds_null_runtime_without_runtime_projection() -> None:
    assert controller_document((controller(),)) == {
        "controller_id": "primary",
        "model": "gpt-5.6-sol",
        "provider": "codex",
        "runtime": None,
        "version": 1,
    }


def test_controller_document_accepts_well_formed_future_provider() -> None:
    future = projection("controller", "primary", {"provider": "future_provider-1"})

    assert controller_document((future,)) == {
        "controller_id": "primary",
        "model": None,
        "provider": "future_provider-1",
        "runtime": None,
        "version": 1,
    }


@pytest.mark.parametrize("provider", ("", "Codex", "contains/path", "a" * 65))
def test_controller_document_rejects_malformed_provider(provider: str) -> None:
    malformed = projection("controller", "primary", {"provider": provider})

    with pytest.raises(StateClientError, match="controller projection is malformed"):
        controller_document((malformed,))


@pytest.mark.parametrize(
    ("status", "reason_code"),
    (
        ("running", None),
        ("stopped", "OPERATOR_SIGNAL"),
        ("crashed", "UNCLEAN_SHUTDOWN"),
    ),
)
def test_controller_document_projects_validated_runtime(
    status: str,
    reason_code: str | None,
) -> None:
    expected: dict[str, JsonValue] = {
        "backend_version": "codex-cli 0.154.0",
        "controller_session_id": SESSION_ID,
        "controller_version": 1,
        "status": status,
    }
    if reason_code is not None:
        expected["reason_code"] = reason_code
    document = controller_document((controller(), runtime(status, reason_code)))
    assert document is not None
    assert document["runtime"] == expected


def test_worker_document_adds_null_execution_without_execution_projection() -> None:
    document = worker_document((controller(), roster(), assignment()))
    workers = document["workers"]
    assert type(workers) is list
    worker = workers[0]
    assert type(worker) is dict
    assignment_document = worker["assignment"]
    assert type(assignment_document) is dict
    assert assignment_document["execution"] is None


@pytest.mark.parametrize(
    ("status", "reason_code"),
    (
        ("claimed", None),
        ("planned", None),
        ("completed", None),
        ("failed", "WORKER_FAILED"),
        ("interrupted", "CONTROLLER_RESTART"),
    ),
)
def test_worker_document_projects_validated_execution(
    status: str,
    reason_code: str | None,
) -> None:
    document = worker_document(
        (controller(), roster(), assignment(), execution(status, reason_code))
    )
    workers = document["workers"]
    assert type(workers) is list
    worker = workers[0]
    assert type(worker) is dict
    assignment_document = worker["assignment"]
    assert type(assignment_document) is dict
    assert assignment_document["execution"] == {
        "assignment_id": ASSIGNMENT_ID,
        "execution_id": EXECUTION_ID,
        "reason_code": reason_code,
        "status": status,
    }


@pytest.mark.parametrize(
    "malform",
    (
        lambda: projection(
            "controller_runtime",
            "primary",
            {"controller_id": "primary", "status": "running"},
        ),
        lambda: runtime("unknown"),
        lambda: projection(
            "controller_runtime",
            "other",
            payload_of(runtime("running")),
        ),
        lambda: projection(
            "controller_runtime",
            "primary",
            {**payload_of(runtime("running")), "controller_version": 2},
        ),
        lambda: runtime("stopped"),
        lambda: runtime("crashed", "OPERATOR_SIGNAL"),
    ),
)
def test_controller_document_rejects_malformed_runtime(
    malform: Callable[[], ProjectionDocument],
) -> None:
    with pytest.raises(StateClientError, match="controller runtime projection is malformed"):
        controller_document((controller(), malform()))


@pytest.mark.parametrize(
    "malform",
    (
        lambda: projection(
            "worker_executions",
            ASSIGNMENT_ID,
            {"assignment_id": ASSIGNMENT_ID, "status": "claimed"},
        ),
        lambda: execution("unknown"),
        lambda: projection(
            "worker_executions",
            "b" * 64,
            payload_of(execution("claimed")),
        ),
        lambda: projection(
            "worker_executions",
            ASSIGNMENT_ID,
            {**payload_of(execution("claimed")), "worker_id": "other"},
        ),
        lambda: execution("claimed", "WORKER_FAILED"),
        lambda: execution("failed"),
        lambda: execution("failed", "CONTROLLER_RESTART"),
    ),
)
def test_worker_document_rejects_malformed_execution(
    malform: Callable[[], ProjectionDocument],
) -> None:
    with pytest.raises(StateClientError, match="worker execution projection is malformed"):
        worker_document((controller(), roster(), assignment(), malform()))


def test_historical_runtime_remains_projectable_after_replay(tmp_path: Path) -> None:
    stopped: dict[str, JsonValue] = {
        "controller_id": "primary",
        "controller_session_id": SESSION_ID,
        "reason_code": "OPERATOR_SIGNAL",
    }
    history = (
        ("ControllerConfigured", payload_of(controller())),
        ("ControllerStarted", payload_of(runtime("running"))),
        ("ControllerStopped", stopped),
    )
    with StateService(StateServiceConfig(tmp_path, "historical-projection")) as state:
        for event_type, payload in history:
            payload.pop("status", None)
            state.append_event(
                AppendEventCommand(event_type, "historical-fixture", None, None, payload)
            )
        expected_events = state.query_events()
        assert state.replay_verify().matched
        assert state.query_events() == expected_events
    records = load_projections(ProjectLayout(tmp_path))
    assert controller_document(records) == controller_document(
        (controller(), runtime("stopped", "OPERATOR_SIGNAL"))
    )
    with StateService(StateServiceConfig(tmp_path, "historical-observer")) as state:
        assert state.query_events() == expected_events
