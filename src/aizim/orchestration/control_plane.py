from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from aizim.domain import AgentRole, sha256_bytes, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.state import AppendEventCommand, StateService

_CONTROLLER_ID: Final = "primary"
_WORKER_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_MAX_TASK_BYTES: Final = 64 * 1024


class ControllerProvider(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"


@dataclass(frozen=True, slots=True)
class ControlPlaneError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return self.reason


def configure_controller(
    state: StateService,
    provider: ControllerProvider,
    model: str | None,
) -> int:
    if type(provider) is not ControllerProvider:
        raise ControlPlaneError("CONTROLLER_PROVIDER_INVALID")
    if model is not None and (type(model) is not str or not model.strip()):
        raise ControlPlaneError("CONTROLLER_MODEL_INVALID")
    payload: dict[str, JsonValue] = {
        "controller_id": _CONTROLLER_ID,
        "provider": provider.value,
    }
    if model is not None:
        payload["model"] = model
    state.append_event(
        AppendEventCommand(
            "ControllerConfigured",
            "operator",
            None,
            None,
            payload,
        )
    )
    record = state.query_projection("controller", _CONTROLLER_ID)
    if record is None:
        raise ControlPlaneError("CONTROL_STATE_INVALID")
    return record.version


def register_worker(
    state: StateService,
    worker_id: str,
    role: AgentRole,
) -> int:
    _validate_worker_id(worker_id)
    if type(role) is not AgentRole:
        raise ControlPlaneError("WORKER_ROLE_INVALID")
    if state.query_projection("worker_roster", worker_id) is not None:
        raise ControlPlaneError("WORKER_ALREADY_REGISTERED")
    state.append_event(
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
    record = state.query_projection("worker_roster", worker_id)
    if record is None:
        raise ControlPlaneError("CONTROL_STATE_INVALID")
    return record.version


def assign_task(state: StateService, worker_id: str, task: str) -> int:
    _validate_worker_id(worker_id)
    _validate_task(task)
    controller = state.query_projection("controller", _CONTROLLER_ID)
    if controller is None:
        raise ControlPlaneError("CONTROLLER_NOT_CONFIGURED")
    if state.query_projection("worker_roster", worker_id) is None:
        raise ControlPlaneError("WORKER_NOT_REGISTERED")
    previous = state.query_projection("worker_assignments", worker_id)
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
    state.append_event(
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
    return task_version


def _validate_worker_id(worker_id: str) -> None:
    if type(worker_id) is not str or _WORKER_ID.fullmatch(worker_id) is None:
        raise ControlPlaneError("WORKER_ID_INVALID")


def _validate_task(task: str) -> None:
    if (
        type(task) is not str
        or not task.strip()
        or len(task.encode()) > _MAX_TASK_BYTES
    ):
        raise ControlPlaneError("WORKER_TASK_INVALID")
