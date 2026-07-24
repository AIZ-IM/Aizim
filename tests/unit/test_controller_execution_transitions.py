from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from aizim.domain import AgentRole
from aizim.domain.serialization import JsonValue
from aizim.orchestration.control_plane import (
    ControllerProvider,
    assign_task,
    configure_controller,
    register_worker,
)
from aizim.orchestration.controller_execution import (
    ControllerExecutionError,
    claim_assignment,
    complete_assignment,
    fail_assignment,
    interrupt_assignment,
    interrupt_nonterminal_executions,
    record_dispatch_planned,
)
from aizim.orchestration.controller_lifecycle import (
    recover_unclean_controller,
    start_controller,
    stop_controller,
)
from aizim.state import (
    EventValidationError,
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
        provider=ControllerProvider.CODEX,
        backend_version="0.145.0",
        executable_hash="c" * 64,
    )


def _configure(state: StateService, *worker_ids: str) -> None:
    configure_controller(state, ControllerProvider.CODEX, "fixture-model")
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


def test_terminal_execution_allows_new_task_version_retry(tmp_path: Path) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, "task-retry")) as state:
        _configure(state, "proof-a")
        first_id = _assignment_id(state, "proof-a", "first task")
        first_claim = claim_assignment(
            state,
            worker_id="proof-a",
            controller_session_id=_SESSION_ID,
            controller_version=1,
            execution_id="execution-1",
        )
        interrupt_assignment(state, first_claim, "OPERATOR_SIGNAL")
        second_id = _assignment_id(state, "proof-a", "second task")

        # When
        second_claim = claim_assignment(
            state,
            worker_id="proof-a",
            controller_session_id=_SESSION_ID,
            controller_version=1,
            execution_id="execution-2",
        )

        # Then
        assert second_id != first_id
        assert second_claim.assignment_id == second_id
        assert second_claim.task_version == 2


def test_completion_before_dispatch_is_rejected_without_event(tmp_path: Path) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, "completion-before-plan")) as state:
        _configure(state, "proof-a")
        _assignment_id(state, "proof-a", "prove the fixture")
        claim = claim_assignment(
            state,
            worker_id="proof-a",
            controller_session_id=_SESSION_ID,
            controller_version=1,
            execution_id="execution-1",
        )
        before = state.query_events()

        # When / Then
        with pytest.raises(ControllerExecutionError):
            complete_assignment(state, claim, "b" * 64)
        assert state.query_events() == before


@pytest.mark.parametrize("planned", [False, True])
def test_failed_assignment_is_terminal_from_each_nonterminal_state(
    tmp_path: Path, planned: bool
) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, f"failed-{planned}")) as state:
        _configure(state, "proof-a")
        assignment_id = _assignment_id(state, "proof-a", "prove the fixture")
        claim = claim_assignment(
            state,
            worker_id="proof-a",
            controller_session_id=_SESSION_ID,
            controller_version=1,
            execution_id="execution-1",
        )
        if planned:
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
        fail_assignment(state, claim, "WORKER_FAILED")
        execution = state.query_projection("worker_executions", assignment_id)

        # Then
        assert execution is not None
        assert _payload(execution)["status"] == "failed"
        assert _payload(execution)["reason_code"] == "WORKER_FAILED"


def test_invalid_failure_reason_appends_no_event(tmp_path: Path) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, "invalid-failure-reason")) as state:
        _configure(state, "proof-a")
        _assignment_id(state, "proof-a", "prove the fixture")
        claim = claim_assignment(
            state,
            worker_id="proof-a",
            controller_session_id=_SESSION_ID,
            controller_version=1,
            execution_id="execution-1",
        )
        before = state.query_events()

        # When / Then
        with pytest.raises(EventValidationError):
            fail_assignment(state, claim, "UNBOUNDED_FAILURE")
        assert state.query_events() == before


def test_recovery_crashes_running_session_once_and_interrupts_in_stable_order(
    tmp_path: Path,
) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, "recovery-source")) as state:
        _configure(state, "proof-b", "proof-a")
        assignment_ids = (
            _assignment_id(state, "proof-b", "task b"),
            _assignment_id(state, "proof-a", "task a"),
        )
        for worker_id, execution_id in (
            ("proof-b", "execution-b"),
            ("proof-a", "execution-a"),
        ):
            claim_assignment(
                state,
                worker_id=worker_id,
                controller_session_id=_SESSION_ID,
                controller_version=1,
                execution_id=execution_id,
            )

    with StateService(StateServiceConfig(tmp_path, "recovery-target")) as restarted:
        # When
        recovered_session = recover_unclean_controller(restarted)
        interrupted = interrupt_nonterminal_executions(restarted)
        second_recovery = recover_unclean_controller(restarted)

        # Then
        assert recovered_session == _SESSION_ID
        assert second_recovery is None
        assert interrupted == tuple(sorted(assignment_ids))
        assert [
            event.envelope.payload["assignment_id"]
            for event in restarted.query_events()
            if event.envelope.event_type == "WorkerTaskInterrupted"
        ] == list(sorted(assignment_ids))
        assert restarted.replay_verify().matched


def test_stopped_controller_accepts_a_new_session(tmp_path: Path) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, "controller-restart")) as state:
        configure_controller(state, ControllerProvider.CODEX, "fixture-model")
        _start(state, "old-session")
        stop_controller(state, "old-session")

        # When
        _start(state, "new-session")
        runtime = state.query_projection("controller_runtime", _CONTROLLER_ID)

        # Then
        assert runtime is not None
        assert _payload(runtime)["controller_session_id"] == "new-session"
        assert _payload(runtime)["status"] == "running"


@pytest.mark.parametrize("crashed", [False, True])
def test_inactive_controller_rejects_dispatch_without_event(tmp_path: Path, crashed: bool) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, f"stale-dispatch-{crashed}")) as state:
        _configure(state, "proof-a")
        _assignment_id(state, "proof-a", "prove the fixture")
        claim = claim_assignment(
            state,
            worker_id="proof-a",
            controller_session_id=_SESSION_ID,
            controller_version=1,
            execution_id="execution-1",
        )
        if crashed:
            assert recover_unclean_controller(state) == _SESSION_ID
        else:
            stop_controller(state, _SESSION_ID)
        before = state.query_events()

        # When / Then
        with pytest.raises(ControllerExecutionError, match="CONTROLLER_NOT_RUNNING"):
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


def test_new_session_rejects_stale_terminal_claim_without_event(tmp_path: Path) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, "stale-terminal")) as state:
        _configure(state, "proof-a")
        _assignment_id(state, "proof-a", "prove the fixture")
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
        stop_controller(state, _SESSION_ID)
        _start(state, "controller-session-2")
        before = state.query_events()

        # When / Then
        with pytest.raises(ControllerExecutionError, match="CONTROLLER_SESSION_STALE"):
            complete_assignment(state, claim, "b" * 64)
        assert state.query_events() == before


def test_second_controller_start_rejects_without_event(tmp_path: Path) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, "already-running")) as state:
        configure_controller(state, ControllerProvider.CODEX, "fixture-model")
        _start(state)
        before = state.query_events()

        # When / Then
        with pytest.raises(ControllerExecutionError, match="CONTROLLER_ALREADY_RUNNING"):
            _start(state, "controller-session-2")
        assert state.query_events() == before
