from __future__ import annotations

import json
from pathlib import Path

import pytest

from aizim.domain.serialization import JsonValue
from aizim.state import AppendEventCommand, EventValidationError, StateService, StateServiceConfig
from aizim.state.store_contracts import ProjectionAuthorityError

_LEGACY_ASSIGNMENT_ID = "a" * 64


def _append_legacy(state: StateService, event_type: str, payload: dict[str, JsonValue]) -> None:
    state.append_event(AppendEventCommand(event_type, "legacy-fixture", None, None, payload))


def _legacy_claim(state: StateService, *, planned: bool = False) -> None:
    _append_legacy(state, "WorkerTaskClaimed", {
        "assignment_id": _LEGACY_ASSIGNMENT_ID,
        "controller_id": "primary",
        "controller_session_id": "legacy-controller-session",
        "controller_version": 1,
        "worker_id": "legacy-proof-a",
        "task_version": 1,
        "execution_id": "legacy-execution-1",
    })
    if planned:
        _append_legacy(state, "WorkerTaskDispatchPlanned", {
            "assignment_id": _LEGACY_ASSIGNMENT_ID,
            "execution_id": "legacy-execution-1",
            "directive_id": "legacy-directive-1",
            "directive_artifact_hash": "d" * 64,
            "instruction_hash": "b" * 64,
            "budget": 12,
            "timeout_milliseconds": 60_000,
        })


def _legacy_start(state: StateService, session_id: str) -> None:
    _append_legacy(state, "ControllerStarted", {
        "controller_id": "primary",
        "controller_session_id": session_id,
        "controller_version": 1,
        "provider": "codex",
        "backend_version": "codex-cli 0.154.0",
        "executable_hash": "c" * 64,
    })


def test_legacy_completion_before_dispatch_rejects_without_mutation(tmp_path: Path) -> None:
    with StateService(StateServiceConfig(tmp_path, "legacy-completion-before-plan")) as state:
        _legacy_claim(state)
        before = state.query_events()
        digest = state.logical_digest()
        with pytest.raises(ProjectionAuthorityError):
            _append_legacy(state, "WorkerTaskCompleted", {
                "assignment_id": _LEGACY_ASSIGNMENT_ID,
                "execution_id": "legacy-execution-1",
                "result_hash": "b" * 64,
            })
        assert state.query_events() == before
        assert state.logical_digest() == digest


@pytest.mark.parametrize("planned", (False, True))
@pytest.mark.parametrize(
    ("event_type", "reason_code", "status"),
    (
        ("WorkerTaskFailed", "WORKER_FAILED", "failed"),
        ("WorkerTaskInterrupted", "CONTROLLER_RESTART", "interrupted"),
    ),
)
def test_legacy_failure_and_interruption_replay_from_each_nonterminal_state(
    tmp_path: Path, planned: bool, event_type: str, reason_code: str, status: str
) -> None:
    with StateService(StateServiceConfig(tmp_path, "legacy-terminal-transition")) as state:
        _legacy_claim(state, planned=planned)
        _append_legacy(state, event_type, {
            "assignment_id": _LEGACY_ASSIGNMENT_ID,
            "execution_id": "legacy-execution-1",
            "reason_code": reason_code,
        })
        record = state.query_projection("worker_executions", _LEGACY_ASSIGNMENT_ID)
        assert record is not None
        payload = json.loads(record.state_json)["payload"]
        assert payload["status"] == status
        assert payload["reason_code"] == reason_code
        assert state.replay_verify().matched
        before = state.query_events()
        with pytest.raises(ProjectionAuthorityError):
            _append_legacy(state, event_type, {
                "assignment_id": _LEGACY_ASSIGNMENT_ID,
                "execution_id": "legacy-execution-1",
                "reason_code": reason_code,
            })
        assert state.query_events() == before


@pytest.mark.parametrize(
    ("event_type", "payload"),
    (
        ("WorkerTaskCompleted", {"result_hash": "b" * 64}),
        ("WorkerTaskFailed", {"reason_code": "WORKER_FAILED"}),
        ("WorkerTaskInterrupted", {"reason_code": "OPERATOR_SIGNAL"}),
    ),
)
def test_legacy_terminal_event_requires_matching_execution_identity(
    tmp_path: Path, event_type: str, payload: dict[str, JsonValue]
) -> None:
    with StateService(StateServiceConfig(tmp_path, "legacy-wrong-execution")) as state:
        _legacy_claim(state, planned=True)
        before = state.query_events()
        with pytest.raises(ProjectionAuthorityError, match="execution_id does not match"):
            _append_legacy(state, event_type, {
                **payload,
                "assignment_id": _LEGACY_ASSIGNMENT_ID,
                "execution_id": "different-legacy-execution",
            })
        assert state.query_events() == before


def test_legacy_invalid_failure_reason_rejects_without_event(tmp_path: Path) -> None:
    with StateService(StateServiceConfig(tmp_path, "legacy-invalid-failure")) as state:
        _legacy_claim(state)
        before = state.query_events()
        with pytest.raises(EventValidationError):
            _append_legacy(state, "WorkerTaskFailed", {
                "assignment_id": _LEGACY_ASSIGNMENT_ID,
                "execution_id": "legacy-execution-1",
                "reason_code": "UNBOUNDED_FAILURE",
            })
        assert state.query_events() == before


@pytest.mark.parametrize(
    ("terminal_event", "reason_code"),
    (("ControllerStopped", "OPERATOR_SIGNAL"), ("ControllerCrashed", "UNCLEAN_SHUTDOWN")),
)
def test_legacy_controller_terminal_session_allows_replayed_restart(
    tmp_path: Path, terminal_event: str, reason_code: str
) -> None:
    with StateService(StateServiceConfig(tmp_path, "legacy-controller-restart")) as state:
        _legacy_start(state, "legacy-old-session")
        _append_legacy(state, terminal_event, {
            "controller_id": "primary",
            "controller_session_id": "legacy-old-session",
            "reason_code": reason_code,
        })
        _legacy_start(state, "legacy-new-session")
        runtime = state.query_projection("controller_runtime", "primary")
        assert runtime is not None
        payload = json.loads(runtime.state_json)["payload"]
        assert payload["controller_session_id"] == "legacy-new-session"
        assert payload["status"] == "running"
        assert payload["provider"] == "codex"
        assert state.replay_verify().matched
        before = state.query_events()
        with pytest.raises(ProjectionAuthorityError, match="controller_session_id does not match"):
            _append_legacy(state, terminal_event, {
                "controller_id": "primary",
                "controller_session_id": "legacy-old-session",
                "reason_code": reason_code,
            })
        assert state.query_events() == before


def test_legacy_duplicate_controller_start_rejects_without_event(tmp_path: Path) -> None:
    with StateService(StateServiceConfig(tmp_path, "legacy-duplicate-start")) as state:
        _legacy_start(state, "legacy-first-session")
        before = state.query_events()
        with pytest.raises(ProjectionAuthorityError):
            _legacy_start(state, "legacy-second-session")
        assert state.query_events() == before
