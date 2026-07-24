from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Final

from aizim.domain import AgentRole
from aizim.domain.serialization import JsonValue
from aizim.orchestration.control_plane import (
    ControllerProvider,
    ControlPlaneError,
)
from aizim.runtime.layout import LayoutError, ProjectLayout
from aizim.state.service import StateServiceLifecycleError

from .control_projection import controller_document, worker_document
from .state_client import (
    StateClientError,
    call_control_operation,
    load_projections,
)

_MESSAGES: Final = {
    "CONTROLLER_NOT_CONFIGURED": "controller is not configured",
    "CONTROLLER_PROVIDER_INVALID": "controller provider is invalid",
    "CONTROLLER_MODEL_INVALID": "controller model is invalid",
    "WORKER_ALREADY_REGISTERED": "worker is already registered",
    "WORKER_NOT_REGISTERED": "worker is not registered",
    "WORKER_ROLE_INVALID": "worker role is invalid",
    "WORKER_ID_INVALID": "worker id is invalid",
    "WORKER_TASK_INVALID": "worker task is invalid",
    "CONTROL_STATE_INVALID": "control state is invalid",
}


def run_controller_configure(
    project: Path,
    provider: ControllerProvider,
    model: str | None,
) -> int:
    layout = _layout(project, "controller")
    if layout is None:
        return 2
    try:
        call_control_operation(
            layout,
            "control.configure_controller",
            {"provider": provider.value, "model": model},
        )
    except ControlPlaneError as error:
        return _domain_failure("controller", error)
    except StateServiceLifecycleError:
        return _busy("controller")
    except (OSError, StateClientError):
        return _state_failure("controller")
    print(f"Configured primary controller with {provider.value}")
    return 0


def run_controller_show(project: Path, as_json: bool) -> int:
    layout = _layout(project, "controller")
    if layout is None:
        return 2
    try:
        controller = controller_document(load_projections(layout))
    except StateClientError:
        return _state_failure("controller")
    if controller is None:
        return _domain_failure("controller", ControlPlaneError("CONTROLLER_NOT_CONFIGURED"))
    if as_json:
        _print_json(controller)
    else:
        model = controller["model"]
        suffix = "" if model is None else f" model={model}"
        runtime = controller["runtime"]
        runtime_status = runtime.get("status") if type(runtime) is dict else "inactive"
        print(f"primary provider={controller['provider']}{suffix} runtime={runtime_status}")
    return 0


def run_worker_register(project: Path, worker_id: str, role: AgentRole) -> int:
    layout = _layout(project, "worker")
    if layout is None:
        return 2
    try:
        call_control_operation(
            layout,
            "control.register_worker",
            {"worker_id": worker_id, "role": role.value},
        )
    except ControlPlaneError as error:
        return _domain_failure("worker", error)
    except StateServiceLifecycleError:
        return _busy("worker")
    except (OSError, StateClientError):
        return _state_failure("worker")
    print(f"Registered worker {worker_id}")
    return 0


def run_worker_assign(project: Path, worker_id: str, task: str) -> int:
    layout = _layout(project, "worker")
    if layout is None:
        return 2
    try:
        task_version = call_control_operation(
            layout,
            "control.assign_task",
            {"worker_id": worker_id, "task": task},
        )
    except ControlPlaneError as error:
        return _domain_failure("worker", error)
    except StateServiceLifecycleError:
        return _busy("worker")
    except (OSError, StateClientError):
        return _state_failure("worker")
    print(f"Assigned task version {task_version} to {worker_id}")
    return 0


def run_worker_list(project: Path, as_json: bool) -> int:
    layout = _layout(project, "worker")
    if layout is None:
        return 2
    try:
        document = worker_document(load_projections(layout))
    except StateClientError:
        return _state_failure("worker")
    if as_json:
        _print_json(document)
        return 0
    controller = document["controller"]
    provider = controller.get("provider") if type(controller) is dict else "unconfigured"
    runtime = controller.get("runtime") if type(controller) is dict else None
    runtime_status = runtime.get("status") if type(runtime) is dict else "inactive"
    print(f"controller {provider} runtime={runtime_status}")
    workers = document["workers"]
    if type(workers) is list:
        for worker in workers:
            if type(worker) is dict:
                assignment = worker.get("assignment")
                task_version = assignment.get("task_version") if type(assignment) is dict else None
                suffix = "" if task_version is None else f" task_version={task_version}"
                execution = assignment.get("execution") if type(assignment) is dict else None
                execution_status = execution.get("status") if type(execution) is dict else "pending"
                print(
                    f"{worker.get('worker_id')} role={worker.get('role')} "
                    f"status={worker.get('status')}{suffix} execution={execution_status}"
                )
    return 0


def _layout(project: Path, command: str) -> ProjectLayout | None:
    try:
        layout = ProjectLayout.from_lean_project(project)
        layout.validate_runtime()
        return layout
    except (LayoutError, OSError):
        print(f"aizim {command}: state is unavailable", file=sys.stderr)
        return None


def _domain_failure(command: str, error: ControlPlaneError) -> int:
    message = _MESSAGES.get(error.code, "control request failed")
    print(f"aizim {command}: {message}", file=sys.stderr)
    return 4


def _busy(command: str) -> int:
    print(f"aizim {command}: state service is busy", file=sys.stderr)
    return 3


def _state_failure(command: str) -> int:
    print(f"aizim {command}: state is unavailable", file=sys.stderr)
    return 6


def _print_json(document: dict[str, JsonValue]) -> None:
    print(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
