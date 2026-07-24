from __future__ import annotations

import json
from typing import Final

from aizim.domain.serialization import JsonValue

from .event_payload import thaw_payload
from .events import EventEnvelope
from .projection_types import ProjectionRecord
from .store_contracts import ProjectionAuthorityError

ORCHESTRATION_EVENT_PROJECTIONS: Final = {
    "ControllerStarted": "controller_runtime",
    "ControllerStopped": "controller_runtime",
    "ControllerCrashed": "controller_runtime",
    "WorkerTaskClaimed": "worker_executions",
    "WorkerTaskDispatchPlanned": "worker_executions",
    "WorkerTaskCompleted": "worker_executions",
    "WorkerTaskFailed": "worker_executions",
    "WorkerTaskInterrupted": "worker_executions",
}
ORCHESTRATION_ENTITY_FIELDS: Final = {
    "ControllerStarted": "controller_id",
    "ControllerStopped": "controller_id",
    "ControllerCrashed": "controller_id",
    "WorkerTaskClaimed": "assignment_id",
    "WorkerTaskDispatchPlanned": "assignment_id",
    "WorkerTaskCompleted": "assignment_id",
    "WorkerTaskFailed": "assignment_id",
    "WorkerTaskInterrupted": "assignment_id",
}
_TARGET_STATUS: Final = {
    "ControllerStarted": "running",
    "ControllerStopped": "stopped",
    "ControllerCrashed": "crashed",
    "WorkerTaskClaimed": "claimed",
    "WorkerTaskDispatchPlanned": "planned",
    "WorkerTaskCompleted": "completed",
    "WorkerTaskFailed": "failed",
    "WorkerTaskInterrupted": "interrupted",
}
_ALLOWED_PREVIOUS: Final = {
    "ControllerStarted": frozenset({None, "stopped", "crashed"}),
    "ControllerStopped": frozenset({"running"}),
    "ControllerCrashed": frozenset({"running"}),
    "WorkerTaskClaimed": frozenset({None}),
    "WorkerTaskDispatchPlanned": frozenset({"claimed"}),
    "WorkerTaskCompleted": frozenset({"planned"}),
    "WorkerTaskFailed": frozenset({"claimed", "planned"}),
    "WorkerTaskInterrupted": frozenset({"claimed", "planned"}),
}


def reduce_orchestration_payload(
    snapshots: tuple[ProjectionRecord, ...],
    event: EventEnvelope,
) -> dict[str, JsonValue]:
    projection_name = ORCHESTRATION_EVENT_PROJECTIONS[event.event_type]
    entity_field = ORCHESTRATION_ENTITY_FIELDS[event.event_type]
    entity_id = event.payload[entity_field]
    if type(entity_id) is not str:
        raise ProjectionAuthorityError(projection_name, "entity identifier is invalid")
    previous = next(
        (
            item
            for item in snapshots
            if item.projection_name == projection_name and item.entity_id == entity_id
        ),
        None,
    )
    previous_payload = _previous_payload(projection_name, previous)
    previous_status = None if previous_payload is None else previous_payload.get("status")
    if previous_status not in _ALLOWED_PREVIOUS[event.event_type]:
        target = _TARGET_STATUS[event.event_type]
        raise ProjectionAuthorityError(
            projection_name,
            f"transition from {previous_status!r} to {target!r} is not allowed",
        )
    payload = thaw_payload(event.payload)
    if previous_payload is not None:
        _require_same_execution(
            projection_name,
            event.event_type,
            previous_payload,
            payload,
        )
        payload = {**previous_payload, **payload}
    payload["status"] = _TARGET_STATUS[event.event_type]
    return payload


def _previous_payload(
    projection_name: str,
    record: ProjectionRecord | None,
) -> dict[str, JsonValue] | None:
    if record is None:
        return None
    state: JsonValue = json.loads(record.state_json)
    if type(state) is not dict:
        raise ProjectionAuthorityError(projection_name, "stored state is invalid")
    payload = state.get("payload")
    if type(payload) is not dict:
        raise ProjectionAuthorityError(projection_name, "stored payload is invalid")
    return payload


def _require_same_execution(
    projection_name: str,
    event_type: str,
    previous: dict[str, JsonValue],
    current: dict[str, JsonValue],
) -> None:
    field = (
        "controller_session_id"
        if event_type in {"ControllerStopped", "ControllerCrashed"}
        else "execution_id"
    )
    if previous.get(field) != current.get(field):
        raise ProjectionAuthorityError(projection_name, f"{field} does not match")
