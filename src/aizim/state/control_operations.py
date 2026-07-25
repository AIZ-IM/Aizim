from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Final, Protocol

from aizim.domain import AgentRole, sha256_bytes, sha256_json
from aizim.domain.controller_provider import is_controller_provider_id
from aizim.domain.serialization import JsonValue

from .operations import AppendEventCommand
from .projections import ProjectionRecord
from .store_contracts import EventRecord

_CONTROLLER_ID: Final = "primary"
_WORKER_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_MAX_TASK_BYTES: Final = 64 * 1024


class ControlOperationTarget(Protocol):
    def append_event(self, command: AppendEventCommand) -> EventRecord: ...

    def query_projection(self, name: str, entity_id: str) -> ProjectionRecord | None: ...


@dataclass(frozen=True, slots=True)
class ControlOperationError(RuntimeError):
    code: str

    def __str__(self) -> str:
        return self.code


def notify_control_committed(callback: Callable[[str], None], operation: str) -> None:
    with suppress(Exception):
        callback(operation)


def configure_controller(target: ControlOperationTarget, provider: str, model: str | None) -> int:
    if not is_controller_provider_id(provider):
        raise ControlOperationError("CONTROLLER_PROVIDER_INVALID")
    if model is not None and (type(model) is not str or not model.strip()):
        raise ControlOperationError("CONTROLLER_MODEL_INVALID")
    payload: dict[str, JsonValue] = {
        "controller_id": _CONTROLLER_ID,
        "provider": provider,
    }
    if model is not None:
        payload["model"] = model
    target.append_event(
        AppendEventCommand(
            "ControllerConfigured",
            "operator",
            None,
            None,
            payload,
        )
    )
    return _projection_version(target, "controller", _CONTROLLER_ID)


def register_worker(target: ControlOperationTarget, worker_id: str, role: AgentRole) -> int:
    _validate_worker_id(worker_id)
    if type(role) is not AgentRole:
        raise ControlOperationError("WORKER_ROLE_INVALID")
    if target.query_projection("worker_roster", worker_id) is not None:
        raise ControlOperationError("WORKER_ALREADY_REGISTERED")
    target.append_event(
        AppendEventCommand(
            "WorkerConfigured",
            "operator",
            None,
            None,
            {
                "worker_id": worker_id,
                "role": role.value,
                "status": "idle",
            },
        )
    )
    return _projection_version(target, "worker_roster", worker_id)


def assign_task(target: ControlOperationTarget, worker_id: str, task: str) -> int:
    _validate_worker_id(worker_id)
    _validate_task(task)
    if target.query_projection("controller", _CONTROLLER_ID) is None:
        raise ControlOperationError("CONTROLLER_NOT_CONFIGURED")
    if target.query_projection("worker_roster", worker_id) is None:
        raise ControlOperationError("WORKER_NOT_REGISTERED")
    previous = target.query_projection("worker_assignments", worker_id)
    task_version = 1 if previous is None else previous.version + 1
    task_hash = sha256_bytes(task.encode())
    assignment_id = sha256_json(
        {
            "controller_id": _CONTROLLER_ID,
            "worker_id": worker_id,
            "task_hash": task_hash,
            "task_version": task_version,
        }
    )
    target.append_event(
        AppendEventCommand(
            "WorkerTaskAssigned",
            _CONTROLLER_ID,
            None,
            None,
            {
                "assignment_id": assignment_id,
                "controller_id": _CONTROLLER_ID,
                "worker_id": worker_id,
                "task": task,
                "task_hash": task_hash,
                "task_version": task_version,
            },
        )
    )
    return _projection_version(target, "worker_assignments", worker_id)


def _projection_version(target: ControlOperationTarget, name: str, entity_id: str) -> int:
    record = target.query_projection(name, entity_id)
    if record is None:
        raise ControlOperationError("CONTROL_STATE_INVALID")
    return record.version


def _validate_worker_id(worker_id: str) -> None:
    if type(worker_id) is not str or _WORKER_ID.fullmatch(worker_id) is None:
        raise ControlOperationError("WORKER_ID_INVALID")


def _validate_task(task: str) -> None:
    if type(task) is not str or not task.strip() or len(task.encode()) > _MAX_TASK_BYTES:
        raise ControlOperationError("WORKER_TASK_INVALID")
