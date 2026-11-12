from __future__ import annotations

import json
from pathlib import Path

import pytest

from aizim.domain import AgentRole
from aizim.domain.serialization import JsonValue
from aizim.state import AppendEventCommand, StateService, StateServiceConfig
from aizim.state.control_operations import assign_task, configure_controller, register_worker


def _append_legacy_execution(state: StateService, terminal_event: str) -> str:
    configure_controller(state, "codex", "legacy-fixture-model")
    register_worker(state, "legacy-proof-a", AgentRole.FORMALIZER)
    assign_task(state, "legacy-proof-a", "prove the historical fixture")
    assignment = state.query_projection("worker_assignments", "legacy-proof-a")
    assert assignment is not None
    assignment_id = json.loads(assignment.state_json)["payload"]["assignment_id"]
    events: list[tuple[str, dict[str, JsonValue]]] = [
        ("ControllerStarted", {
            "controller_id": "primary",
            "controller_session_id": "legacy-controller-session",
            "controller_version": 1,
            "provider": "codex",
            "backend_version": "codex-cli 0.154.0",
            "executable_hash": "c" * 64,
        }),
        ("WorkerTaskClaimed", {
            "assignment_id": assignment_id,
            "controller_id": "primary",
            "controller_session_id": "legacy-controller-session",
            "controller_version": 1,
            "worker_id": "legacy-proof-a",
            "task_version": 1,
            "execution_id": "legacy-execution-1",
        }),
        ("WorkerTaskDispatchPlanned", {
            "assignment_id": assignment_id,
            "execution_id": "legacy-execution-1",
            "directive_id": "legacy-directive-1",
            "directive_artifact_hash": "d" * 64,
            "instruction_hash": "a" * 64,
            "budget": 12,
            "timeout_milliseconds": 60_000,
        }),
    ]
    terminal_payload: dict[str, JsonValue] = {
        "assignment_id": assignment_id,
        "execution_id": "legacy-execution-1",
    }
    if terminal_event == "WorkerTaskCompleted":
        terminal_payload["result_hash"] = "b" * 64
    else:
        terminal_payload["reason_code"] = (
            "WORKER_FAILED" if terminal_event == "WorkerTaskFailed" else "CONTROLLER_RESTART"
        )
    events.append((terminal_event, terminal_payload))
    events.append(("ControllerStopped", {
        "controller_id": "primary",
        "controller_session_id": "legacy-controller-session",
        "reason_code": "OPERATOR_SIGNAL",
    }))
    for event_type, payload in events:
        state.append_event(AppendEventCommand(event_type, "legacy-fixture", None, None, payload))
    return assignment_id


@pytest.mark.parametrize(
    ("terminal_event", "status"),
    (
        ("WorkerTaskCompleted", "completed"),
        ("WorkerTaskFailed", "failed"),
        ("WorkerTaskInterrupted", "interrupted"),
    ),
)
def test_legacy_terminal_execution_replays_without_rewriting_identity(
    tmp_path: Path, terminal_event: str, status: str
) -> None:
    with StateService(StateServiceConfig(tmp_path, "legacy-replay-source")) as state:
        assignment_id = _append_legacy_execution(state, terminal_event)
        expected_projection = state.canonical_projection_json()
        expected_digest = state.logical_digest()
        expected_events = state.query_events()

    with StateService(StateServiceConfig(tmp_path, "legacy-replay-restarted")) as restarted:
        execution = restarted.query_projection("worker_executions", assignment_id)
        runtime = restarted.query_projection("controller_runtime", "primary")
        controller = restarted.query_projection("controller", "primary")
        replay = restarted.replay_verify()
        assert restarted.query_events() == expected_events

    assert execution is not None and runtime is not None and controller is not None
    execution_payload = json.loads(execution.state_json)["payload"]
    assert execution_payload["status"] == status
    assert execution_payload["controller_session_id"] == "legacy-controller-session"
    assert execution_payload["execution_id"] == "legacy-execution-1"
    assert execution_payload["directive_artifact_hash"] == "d" * 64
    if status == "completed":
        assert execution_payload["result_hash"] == "b" * 64
    runtime_payload = json.loads(runtime.state_json)["payload"]
    assert runtime_payload["provider"] == "codex"
    assert runtime_payload["backend_version"] == "codex-cli 0.154.0"
    assert runtime_payload["executable_hash"] == "c" * 64
    assert runtime_payload["status"] == "stopped"
    assert json.loads(controller.state_json)["payload"]["model"] == "legacy-fixture-model"
    assert replay.matched
    assert replay.projection_json == expected_projection
    assert replay.logical_digest == expected_digest
