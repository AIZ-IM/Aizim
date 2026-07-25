from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal, assert_never

from aizim.domain.controller_provider import is_controller_provider_id
from aizim.domain.serialization import JsonValue

from .state_client import ProjectionDocument, StateClientError

_FAILED_REASONS = (
    "CONTROLLER_BLOCKED",
    "CONTROLLER_REJECTED",
    "CONTROLLER_DECISION_INVALID",
    "CONTROLLER_TIMEOUT",
    "WORKER_FAILED",
    "WORKER_ROLE_UNSUPPORTED",
)
type RuntimeStatus = Literal["running", "stopped", "crashed"]
type ExecutionStatus = Literal["claimed", "planned", "completed", "failed", "interrupted"]
type _ExecutionProjection = tuple[str, str, str | None, ExecutionStatus, int, str]
_RUNTIME_STATUSES: Mapping[str, RuntimeStatus] = {
    "running": "running",
    "stopped": "stopped",
    "crashed": "crashed",
}
_EXECUTION_STATUSES: Mapping[str, ExecutionStatus] = {
    "claimed": "claimed",
    "planned": "planned",
    "completed": "completed",
    "failed": "failed",
    "interrupted": "interrupted",
}


def controller_document(
    records: tuple[ProjectionDocument, ...],
) -> dict[str, JsonValue] | None:
    record = next(
        (
            item
            for item in records
            if (item.projection_name, item.entity_id) == ("controller", "primary")
        ),
        None,
    )
    if record is None:
        return None
    payload = _payload(record)
    provider = payload.get("provider")
    model = payload.get("model")
    if not is_controller_provider_id(provider) or (model is not None and type(model) is not str):
        raise StateClientError("controller projection is malformed")
    runtime = _controller_runtime(records, record.version, provider)
    return {
        "controller_id": "primary",
        "model": model,
        "provider": provider,
        "runtime": runtime,
        "version": record.version,
    }


def worker_document(
    records: tuple[ProjectionDocument, ...],
) -> dict[str, JsonValue]:
    assignments = {
        record.entity_id: record
        for record in records
        if record.projection_name == "worker_assignments"
    }
    executions = {
        parsed[0]: parsed
        for record in records
        if record.projection_name == "worker_executions"
        for parsed in (_execution_projection(record),)
    }
    workers: list[JsonValue] = []
    for record in sorted(
        (item for item in records if item.projection_name == "worker_roster"),
        key=lambda item: item.entity_id,
    ):
        payload = _payload(record)
        role, status = payload.get("role"), payload.get("status")
        if type(role) is not str or type(status) is not str:
            raise StateClientError("worker projection is malformed")
        assignment_record = assignments.get(record.entity_id)
        assignment = (
            None
            if assignment_record is None
            else _assignment_document(assignment_record, executions, record.entity_id)
        )
        workers.append(
            {
                "assignment": assignment,
                "role": role,
                "status": status,
                "version": record.version,
                "worker_id": record.entity_id,
            }
        )
    return {"controller": controller_document(records), "workers": workers}


def _assignment_document(
    record: ProjectionDocument,
    executions: Mapping[str, _ExecutionProjection],
    worker_id: str,
) -> dict[str, JsonValue]:
    payload = _payload(record)
    assignment_id = payload.get("assignment_id")
    task, task_hash = payload.get("task"), payload.get("task_hash")
    task_version = payload.get("task_version")
    if (
        type(assignment_id) is not str
        or payload.get("controller_id") != "primary"
        or type(task) is not str
        or type(task_hash) is not str
        or type(task_version) is not int
        or task_version <= 0
    ):
        raise StateClientError("worker assignment projection is malformed")
    execution = executions.get(assignment_id)
    if execution is not None and (
        execution[0] != assignment_id or execution[5] != worker_id or execution[4] != task_version
    ):
        raise StateClientError("worker execution projection is malformed")
    return {
        "assignment_id": assignment_id,
        "controller_id": "primary",
        "execution": None if execution is None else _execution_document(execution),
        "task": task,
        "task_hash": task_hash,
        "task_version": task_version,
    }


def _controller_runtime(
    records: tuple[ProjectionDocument, ...],
    controller_version: int,
    provider: str,
) -> dict[str, JsonValue] | None:
    runtime_records = tuple(
        record for record in records if record.projection_name == "controller_runtime"
    )
    if not runtime_records:
        return None
    if len(runtime_records) != 1:
        raise StateClientError("controller runtime projection is malformed")
    record = runtime_records[0]
    payload = _payload(record)
    session_id = payload.get("controller_session_id")
    version = payload.get("controller_version")
    backend_version = payload.get("backend_version")
    status = _runtime_status(payload.get("status"))
    if (
        record.entity_id != "primary"
        or payload.get("controller_id") != "primary"
        or payload.get("provider") != provider
        or type(session_id) is not str
        or re.fullmatch(r"controller-[0-9a-f]{32}", session_id) is None
        or type(version) is not int
        or version <= 0
        or version != controller_version
        or type(backend_version) is not str
        or not backend_version
    ):
        raise StateClientError("controller runtime projection is malformed")
    reason_code = _runtime_reason(status, payload.get("reason_code"))
    result: dict[str, JsonValue] = {
        "backend_version": backend_version,
        "controller_session_id": session_id,
        "controller_version": version,
        "status": status,
    }
    if reason_code is not None:
        result["reason_code"] = reason_code
    return result


def _runtime_status(value: JsonValue | None) -> RuntimeStatus:
    if type(value) is not str or (status := _RUNTIME_STATUSES.get(value)) is None:
        raise StateClientError("controller runtime projection is malformed")
    return status


def _runtime_reason(status: RuntimeStatus, value: JsonValue | None) -> str | None:
    match status:
        case "running":
            valid = value is None
        case "stopped":
            valid = value in {"OPERATOR_SIGNAL", "PREFLIGHT_FAILED", "CONTROLLER_FAILED"}
        case "crashed":
            valid = value == "UNCLEAN_SHUTDOWN"
        case unreachable:
            assert_never(unreachable)
    if not valid:
        raise StateClientError("controller runtime projection is malformed")
    return value if type(value) is str else None


def _execution_projection(record: ProjectionDocument) -> _ExecutionProjection:
    payload = _payload(record)
    assignment_id = payload.get("assignment_id")
    controller_version = payload.get("controller_version")
    controller_session_id = payload.get("controller_session_id")
    execution_id, worker_id = payload.get("execution_id"), payload.get("worker_id")
    task_version = payload.get("task_version")
    status = _execution_status(payload.get("status"))
    if (
        type(assignment_id) is not str
        or re.fullmatch(r"[0-9a-f]{64}", assignment_id) is None
        or record.entity_id != assignment_id
        or payload.get("controller_id") != "primary"
        or type(controller_session_id) is not str
        or re.fullmatch(r"controller-[0-9a-f]{32}", controller_session_id) is None
        or type(controller_version) is not int
        or controller_version <= 0
        or type(worker_id) is not str
        or not worker_id
        or type(task_version) is not int
        or task_version <= 0
        or type(execution_id) is not str
        or re.fullmatch(r"execution-[0-9a-f]{32}", execution_id) is None
    ):
        raise StateClientError("worker execution projection is malformed")
    return (
        assignment_id,
        execution_id,
        _execution_reason(status, payload.get("reason_code")),
        status,
        task_version,
        worker_id,
    )


def _execution_status(value: JsonValue | None) -> ExecutionStatus:
    if type(value) is not str or (status := _EXECUTION_STATUSES.get(value)) is None:
        raise StateClientError("worker execution projection is malformed")
    return status


def _execution_reason(status: ExecutionStatus, value: JsonValue | None) -> str | None:
    match status:
        case "claimed" | "planned" | "completed":
            valid = value is None
        case "failed":
            valid = value in _FAILED_REASONS
        case "interrupted":
            valid = value in {"OPERATOR_SIGNAL", "CONTROLLER_RESTART"}
        case unreachable:
            assert_never(unreachable)
    if not valid:
        raise StateClientError("worker execution projection is malformed")
    return value if type(value) is str else None


def _execution_document(execution: _ExecutionProjection) -> dict[str, JsonValue]:
    return dict(
        assignment_id=execution[0],
        execution_id=execution[1],
        reason_code=execution[2],
        status=execution[3],
    )


def _payload(record: ProjectionDocument) -> dict[str, JsonValue]:
    payload = record.state.get("payload")
    if type(payload) is not dict:
        raise StateClientError("control projection is malformed")
    return payload
