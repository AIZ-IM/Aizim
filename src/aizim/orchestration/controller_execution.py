from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Final

from aizim.domain import AgentRole
from aizim.domain.serialization import JsonValue
from aizim.state import AppendEventCommand, ProjectionRecord, StateService

_CONTROLLER_ID: Final = "primary"
_NONTERMINAL: Final = frozenset({"claimed", "planned"})


@dataclass(frozen=True, slots=True)
class ControllerExecutionError(RuntimeError):
    code: str

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class ClaimedAssignment:
    assignment_id: str
    worker_id: str
    role: AgentRole
    task: str = field(repr=False)
    task_version: int
    execution_id: str
    controller_version: int


def claim_assignment(
    state: StateService,
    *,
    worker_id: str,
    controller_session_id: str,
    controller_version: int,
    execution_id: str,
) -> ClaimedAssignment:
    assignment = state.query_projection("worker_assignments", worker_id)
    if assignment is None:
        raise ControllerExecutionError("ASSIGNMENT_NOT_FOUND")
    assignment_payload = _payload(assignment)
    task_version = _integer(assignment_payload, "task_version")
    if assignment.version != task_version:
        raise ControllerExecutionError("TASK_VERSION_STALE")
    configured = state.query_projection("controller", _CONTROLLER_ID)
    if configured is None or configured.version != controller_version:
        raise ControllerExecutionError("CONTROLLER_VERSION_STALE")
    runtime = state.query_projection("controller_runtime", _CONTROLLER_ID)
    if runtime is None:
        raise ControllerExecutionError("CONTROLLER_NOT_RUNNING")
    runtime_payload = _payload(runtime)
    if runtime_payload.get("status") != "running":
        raise ControllerExecutionError("CONTROLLER_NOT_RUNNING")
    if runtime_payload.get("controller_session_id") != controller_session_id:
        raise ControllerExecutionError("CONTROLLER_SESSION_STALE")
    if runtime_payload.get("controller_version") != controller_version:
        raise ControllerExecutionError("CONTROLLER_VERSION_STALE")
    roster = state.query_projection("worker_roster", worker_id)
    if roster is None:
        raise ControllerExecutionError("WORKER_NOT_REGISTERED")
    role = _role(_payload(roster))
    assignment_id = _text(assignment_payload, "assignment_id")
    if state.query_projection("worker_executions", assignment_id) is not None:
        raise ControllerExecutionError("ASSIGNMENT_ALREADY_CLAIMED")
    executions = state.projections("worker_executions")
    if any(_active_for_worker(record, worker_id) for record in executions):
        raise ControllerExecutionError("WORKER_EXECUTION_ACTIVE")
    if any(lease.worker_id == worker_id for lease in state.active_document_leases()):
        raise ControllerExecutionError("WORKER_LEASE_ACTIVE")
    claim = ClaimedAssignment(
        assignment_id=assignment_id,
        worker_id=worker_id,
        role=role,
        task=_text(assignment_payload, "task"),
        task_version=task_version,
        execution_id=execution_id,
        controller_version=controller_version,
    )
    state.append_event(
        AppendEventCommand(
            "WorkerTaskClaimed",
            _CONTROLLER_ID,
            None,
            None,
            {
                "assignment_id": claim.assignment_id,
                "controller_id": _CONTROLLER_ID,
                "controller_session_id": controller_session_id,
                "controller_version": claim.controller_version,
                "worker_id": claim.worker_id,
                "task_version": claim.task_version,
                "execution_id": claim.execution_id,
            },
        )
    )
    return claim


def record_dispatch_planned(
    state: StateService,
    claim: ClaimedAssignment,
    *,
    directive_id: str,
    directive_artifact_hash: str,
    instruction_hash: str,
    budget: int,
    timeout_milliseconds: int,
) -> None:
    _validate_claim(state, claim, frozenset({"claimed"}))
    state.append_event(
        AppendEventCommand(
            "WorkerTaskDispatchPlanned",
            _CONTROLLER_ID,
            None,
            None,
            {
                "assignment_id": claim.assignment_id,
                "execution_id": claim.execution_id,
                "directive_id": directive_id,
                "directive_artifact_hash": directive_artifact_hash,
                "instruction_hash": instruction_hash,
                "budget": budget,
                "timeout_milliseconds": timeout_milliseconds,
            },
        )
    )


def complete_assignment(state: StateService, claim: ClaimedAssignment, result_hash: str) -> None:
    _append_terminal(
        state,
        claim,
        "WorkerTaskCompleted",
        {"result_hash": result_hash},
        frozenset({"planned"}),
    )


def fail_assignment(state: StateService, claim: ClaimedAssignment, reason_code: str) -> None:
    _append_terminal(state, claim, "WorkerTaskFailed", {"reason_code": reason_code}, _NONTERMINAL)


def interrupt_assignment(state: StateService, claim: ClaimedAssignment, reason_code: str) -> None:
    _append_terminal(
        state,
        claim,
        "WorkerTaskInterrupted",
        {"reason_code": reason_code},
        _NONTERMINAL,
    )


def interrupt_nonterminal_executions(state: StateService) -> tuple[str, ...]:
    records = sorted(state.projections("worker_executions"), key=lambda item: item.entity_id)
    interrupted: list[str] = []
    for record in records:
        payload = _payload(record)
        if payload.get("status") not in _NONTERMINAL:
            continue
        assignment_id = _text(payload, "assignment_id")
        state.append_event(
            AppendEventCommand(
                "WorkerTaskInterrupted",
                _CONTROLLER_ID,
                None,
                None,
                {
                    "assignment_id": assignment_id,
                    "execution_id": _text(payload, "execution_id"),
                    "reason_code": "CONTROLLER_RESTART",
                },
            )
        )
        interrupted.append(assignment_id)
    return tuple(interrupted)


def _append_terminal(
    state: StateService,
    claim: ClaimedAssignment,
    event_type: str,
    terminal: dict[str, JsonValue],
    allowed_statuses: frozenset[str],
) -> None:
    _validate_claim(state, claim, allowed_statuses)
    state.append_event(
        AppendEventCommand(
            event_type,
            _CONTROLLER_ID,
            None,
            None,
            {
                "assignment_id": claim.assignment_id,
                "execution_id": claim.execution_id,
                **terminal,
            },
        )
    )


def _validate_claim(
    state: StateService,
    claim: ClaimedAssignment,
    allowed_statuses: frozenset[str],
) -> None:
    current = state.query_projection("worker_assignments", claim.worker_id)
    if current is None:
        raise ControllerExecutionError("ASSIGNMENT_NOT_FOUND")
    current_payload = _payload(current)
    if (
        current.version != claim.task_version
        or current_payload.get("task_version") != claim.task_version
        or current_payload.get("assignment_id") != claim.assignment_id
    ):
        raise ControllerExecutionError("TASK_VERSION_STALE")
    execution = state.query_projection("worker_executions", claim.assignment_id)
    if execution is None:
        raise ControllerExecutionError("EXECUTION_NOT_FOUND")
    execution_payload = _payload(execution)
    if execution_payload.get("execution_id") != claim.execution_id:
        raise ControllerExecutionError("EXECUTION_ID_STALE")
    configured = state.query_projection("controller", _CONTROLLER_ID)
    if configured is None or configured.version != claim.controller_version:
        raise ControllerExecutionError("CONTROLLER_VERSION_STALE")
    runtime = state.query_projection("controller_runtime", _CONTROLLER_ID)
    if runtime is None:
        raise ControllerExecutionError("CONTROLLER_NOT_RUNNING")
    runtime_payload = _payload(runtime)
    if runtime_payload.get("status") != "running":
        raise ControllerExecutionError("CONTROLLER_NOT_RUNNING")
    if _text(execution_payload, "controller_session_id") != _text(
        runtime_payload, "controller_session_id"
    ):
        raise ControllerExecutionError("CONTROLLER_SESSION_STALE")
    if (
        _integer(execution_payload, "controller_version") != configured.version
        or _integer(runtime_payload, "controller_version") != configured.version
    ):
        raise ControllerExecutionError("CONTROLLER_VERSION_STALE")
    if execution_payload.get("status") not in allowed_statuses:
        raise ControllerExecutionError("EXECUTION_TERMINAL")


def _active_for_worker(record: ProjectionRecord, worker_id: str) -> bool:
    payload = _payload(record)
    return payload.get("worker_id") == worker_id and payload.get("status") in _NONTERMINAL


def _payload(record: ProjectionRecord) -> dict[str, JsonValue]:
    state: JsonValue = json.loads(record.state_json)
    payload = state.get("payload") if type(state) is dict else None
    if type(payload) is not dict:
        raise ControllerExecutionError("EXECUTION_STATE_INVALID")
    return payload


def _text(payload: dict[str, JsonValue], field_name: str) -> str:
    value = payload.get(field_name)
    if type(value) is not str or not value:
        raise ControllerExecutionError("EXECUTION_STATE_INVALID")
    return value


def _integer(payload: dict[str, JsonValue], field_name: str) -> int:
    value = payload.get(field_name)
    if type(value) is not int or value <= 0:
        raise ControllerExecutionError("EXECUTION_STATE_INVALID")
    return value


def _role(payload: dict[str, JsonValue]) -> AgentRole:
    value = payload.get("role")
    if type(value) is not str:
        raise ControllerExecutionError("WORKER_ROLE_INVALID")
    try:
        return AgentRole(value)
    except ValueError:
        raise ControllerExecutionError("WORKER_ROLE_INVALID") from None
