from __future__ import annotations

from aizim.domain import AgentRole, ControllerProviderId
from aizim.state.control_operations import (
    ControlOperationError as ControlPlaneError,
)
from aizim.state.control_operations import (
    ControlOperationTarget,
)
from aizim.state.control_operations import (
    assign_task as _assign_task,
)
from aizim.state.control_operations import (
    configure_controller as _configure_controller,
)
from aizim.state.control_operations import (
    register_worker as _register_worker,
)

__all__ = [
    "ControlPlaneError",
    "ControllerProviderId",
    "assign_task",
    "configure_controller",
    "register_worker",
]


def configure_controller(
    target: ControlOperationTarget,
    provider: ControllerProviderId,
    model: str | None,
) -> int:
    value = provider.value if type(provider) is ControllerProviderId else ""
    return _configure_controller(target, value, model)


def register_worker(
    target: ControlOperationTarget,
    worker_id: str,
    role: AgentRole,
) -> int:
    return _register_worker(target, worker_id, role)


def assign_task(target: ControlOperationTarget, worker_id: str, task: str) -> int:
    return _assign_task(target, worker_id, task)
