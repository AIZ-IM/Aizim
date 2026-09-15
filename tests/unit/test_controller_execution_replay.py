from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from aizim.domain import AgentRole, ControllerProviderId
from aizim.domain.serialization import JsonValue
from aizim.orchestration.control_plane import (
    assign_task,
    configure_controller,
    register_worker,
)
from aizim.orchestration.controller_execution import (
    ControllerExecutionError,
    claim_assignment,
    complete_assignment,
    record_dispatch_planned,
)
from aizim.orchestration.controller_lifecycle import start_controller
from aizim.state import (
    AppendEventCommand,
    ProjectionRecord,
    StateService,
    StateServiceConfig,
)

_CONTROLLER_ID: Final = "primary"
_SESSION_ID: Final = "controller-session-1"


def _payload(record: ProjectionRecord) -> dict[str, JsonValue]:
    document: JsonValue = json.loads(record.state_json)
    assert type(document) is dict
    payload = document.get("payload")
    assert type(payload) is dict
    return payload


def _start(state: StateService, session_id: str = _SESSION_ID) -> None:
    start_controller(
        state,
        session_id=session_id,
        controller_version=1,
        provider=ControllerProviderId("codex"),
        backend_version="0.154.0",
        executable_hash="c" * 64,
    )


def _configure(state: StateService, *worker_ids: str) -> None:
    configure_controller(state, ControllerProviderId("codex"), "fixture-model")
    for worker_id in worker_ids:
        register_worker(state, worker_id, AgentRole.FORMALIZER)
    _start(state)


def _assignment_id(state: StateService, worker_id: str, task: str) -> str:
    assign_task(state, worker_id, task)
    assignment = state.query_projection("worker_assignments", worker_id)
    assert assignment is not None
    assignment_id = _payload(assignment)["assignment_id"]
    assert type(assignment_id) is str
    return assignment_id


def test_terminal_assignment_execution_replays_after_restart(tmp_path: Path) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, "controller-replay")) as state:
        _configure(state, "proof-a")
        assignment_id = _assignment_id(state, "proof-a", "prove the fixture")
        claim = claim_assignment(
            state,
            worker_id="proof-a",
            controller_session_id=_SESSION_ID,
            controller_version=1,
            execution_id="execution-1",
        )
        record_dispatch_planned(
            state,
            claim,
            directive_id="directive-1",
            directive_artifact_hash="d" * 64,
            instruction_hash="a" * 64,
            budget=12,
            timeout_milliseconds=60_000,
        )

        # When
        complete_assignment(state, claim, "b" * 64)
        expected_digest = state.logical_digest()

    with StateService(StateServiceConfig(tmp_path, "controller-restarted")) as restarted:
        execution = restarted.query_projection("worker_executions", assignment_id)
        replay = restarted.replay_verify()

    # Then
    assert execution is not None
    assert _payload(execution) == {
        "assignment_id": assignment_id,
        "budget": 12,
        "controller_id": "primary",
        "controller_session_id": "controller-session-1",
        "controller_version": 1,
        "directive_artifact_hash": "d" * 64,
        "directive_id": "directive-1",
        "execution_id": "execution-1",
        "instruction_hash": "a" * 64,
        "result_hash": "b" * 64,
        "status": "completed",
        "task_version": 1,
        "timeout_milliseconds": 60_000,
        "worker_id": "proof-a",
    }
    assert replay.matched
    assert replay.logical_digest == expected_digest


def test_stale_controller_version_rejects_claim_without_event(tmp_path: Path) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, "stale-controller")) as state:
        _configure(state, "proof-a")
        _assignment_id(state, "proof-a", "prove the fixture")
        before = state.query_events()

        # When / Then
        with pytest.raises(ControllerExecutionError, match="CONTROLLER_VERSION_STALE"):
            claim_assignment(
                state,
                worker_id="proof-a",
                controller_session_id=_SESSION_ID,
                controller_version=2,
                execution_id="execution-1",
            )
        assert state.query_events() == before


def test_stale_controller_start_uses_execution_error_without_event(tmp_path: Path) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, "stale-controller-start")) as state:
        configure_controller(state, ControllerProviderId("codex"), "fixture-model")
        before = state.query_events()

        # When / Then
        with pytest.raises(ControllerExecutionError, match="CONTROLLER_VERSION_STALE"):
            start_controller(
                state,
                session_id=_SESSION_ID,
                controller_version=2,
                provider=ControllerProviderId("codex"),
                backend_version="0.154.0",
                executable_hash="c" * 64,
            )
        assert state.query_events() == before


def test_stale_task_version_rejects_dispatch_without_event(tmp_path: Path) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, "stale-task")) as state:
        _configure(state, "proof-a")
        _assignment_id(state, "proof-a", "first task")
        claim = claim_assignment(
            state,
            worker_id="proof-a",
            controller_session_id=_SESSION_ID,
            controller_version=1,
            execution_id="execution-1",
        )
        _assignment_id(state, "proof-a", "replacement task")
        before = state.query_events()

        # When / Then
        with pytest.raises(ControllerExecutionError, match="TASK_VERSION_STALE"):
            record_dispatch_planned(
                state,
                claim,
                directive_id="directive-1",
                directive_artifact_hash="d" * 64,
                instruction_hash="a" * 64,
                budget=12,
                timeout_milliseconds=60_000,
            )
        assert state.query_events() == before


def test_duplicate_claim_appends_no_event(tmp_path: Path) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, "duplicate-claim")) as state:
        _configure(state, "proof-a")
        _assignment_id(state, "proof-a", "prove the fixture")
        claim_assignment(
            state,
            worker_id="proof-a",
            controller_session_id=_SESSION_ID,
            controller_version=1,
            execution_id="execution-1",
        )
        before = state.query_events()

        # When / Then
        with pytest.raises(ControllerExecutionError, match="ASSIGNMENT_ALREADY_CLAIMED"):
            claim_assignment(
                state,
                worker_id="proof-a",
                controller_session_id=_SESSION_ID,
                controller_version=1,
                execution_id="execution-2",
            )
        assert state.query_events() == before


def test_another_active_execution_rejects_claim_without_event(tmp_path: Path) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, "active-execution")) as state:
        _configure(state, "proof-a")
        _assignment_id(state, "proof-a", "first task")
        claim_assignment(
            state,
            worker_id="proof-a",
            controller_session_id=_SESSION_ID,
            controller_version=1,
            execution_id="execution-1",
        )
        _assignment_id(state, "proof-a", "second task")
        before = state.query_events()

        # When / Then
        with pytest.raises(ControllerExecutionError, match="WORKER_EXECUTION_ACTIVE"):
            claim_assignment(
                state,
                worker_id="proof-a",
                controller_session_id=_SESSION_ID,
                controller_version=1,
                execution_id="execution-2",
            )
        assert state.query_events() == before


def test_active_document_lease_rejects_claim_without_event(tmp_path: Path) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, "active-lease")) as state:
        _configure(state, "proof-a")
        _assignment_id(state, "proof-a", "prove the fixture")
        state.append_event(
            AppendEventCommand(
                "LeaseGranted",
                "document_broker",
                "run-1",
                None,
                {
                    "document_id": "document-1",
                    "lease_id": "lease-1",
                    "worker_id": "proof-a",
                    "relative_path": "AizimSmoke/Workers/run-1/proof-a.lean",
                    "virtual_document_namespace": "AizimSmoke.Workers.ProofA",
                    "base_epoch": "a" * 64,
                    "knowledge_epoch": 0,
                    "version": 0,
                    "content_hash": "b" * 64,
                    "expires_at": "2026-07-24T12:00:00Z",
                },
            )
        )
        before = state.query_events()

        # When / Then
        with pytest.raises(ControllerExecutionError, match="WORKER_LEASE_ACTIVE"):
            claim_assignment(
                state,
                worker_id="proof-a",
                controller_session_id=_SESSION_ID,
                controller_version=1,
                execution_id="execution-1",
            )
        assert state.query_events() == before
