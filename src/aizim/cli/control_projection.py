from __future__ import annotations

from aizim.domain.serialization import JsonValue

from .state_client import ProjectionDocument, StateClientError


def controller_document(
    records: tuple[ProjectionDocument, ...],
) -> dict[str, JsonValue] | None:
    record = next(
        (
            item
            for item in records
            if item.projection_name == "controller" and item.entity_id == "primary"
        ),
        None,
    )
    if record is None:
        return None
    payload = _payload(record)
    provider = payload.get("provider")
    model = payload.get("model")
    if (
        type(provider) is not str
        or provider not in {"codex", "claude"}
        or (model is not None and type(model) is not str)
    ):
        raise StateClientError("controller projection is malformed")
    return {
        "controller_id": "primary",
        "model": model,
        "provider": provider,
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
            None if assignment_record is None else _assignment_document(assignment_record)
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


def _assignment_document(record: ProjectionDocument) -> dict[str, JsonValue]:
    payload = _payload(record)
    assignment_id = payload.get("assignment_id")
    controller_id = payload.get("controller_id")
    task = payload.get("task")
    task_hash = payload.get("task_hash")
    task_version = payload.get("task_version")
    if (
        type(assignment_id) is not str
        or type(controller_id) is not str
        or type(task) is not str
        or type(task_hash) is not str
        or type(task_version) is not int
        or task_version <= 0
    ):
        raise StateClientError("worker assignment projection is malformed")
    return {
        "assignment_id": assignment_id,
        "controller_id": controller_id,
        "task": task,
        "task_hash": task_hash,
        "task_version": task_version,
    }


def _payload(record: ProjectionDocument) -> dict[str, JsonValue]:
    payload = record.state.get("payload")
    if type(payload) is not dict:
        raise StateClientError("control projection is malformed")
    return payload
