from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol

from jsonschema import Draft202012Validator, ValidationError

from aizim.agents import BackendIdentity
from aizim.domain import AgentRole, canonical_json
from aizim.domain.serialization import JsonValue

MAX_CONTROLLER_INSTRUCTION_BYTES: Final = 64 * 1024
CONTROLLER_PLAN_TIMEOUT_SECONDS: Final = 60.0
MAX_WORKER_BUDGET: Final = 12
MAX_WORKER_TIMEOUT_SECONDS: Final = 60.0
_SCHEMA_PATH: Final = Path(__file__).with_name("controller_decision.schema.json")
_DECISION_VALIDATOR = Draft202012Validator(json.loads(_SCHEMA_PATH.read_text()))

type ControllerBackendErrorCode = Literal[
    "CONTROLLER_DECISION_INVALID",
    "CONTROLLER_WORKER_INVALID",
    "CONTROLLER_INSTRUCTION_INVALID",
    "CONTROLLER_BUDGET_INVALID",
    "CONTROLLER_TIMEOUT_INVALID",
]
type ControllerDecision = DispatchDecision | BlockedDecision | RejectDecision
type ControllerOperation = Literal[
    "project.read",
    "lean.goal",
    "lean.multi_attempt",
    "lean.diagnostics",
    "document.apply",
    "contribution.submit",
    "knowledge.read",
]
_SAFE_CONTROLLER_OPERATIONS: Final[frozenset[ControllerOperation]] = frozenset(
    {
        "project.read",
        "lean.goal",
        "lean.multi_attempt",
        "lean.diagnostics",
        "document.apply",
        "contribution.submit",
        "knowledge.read",
    }
)


@dataclass(frozen=True, slots=True)
class ControllerBackendError(RuntimeError):
    code: ControllerBackendErrorCode

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class DispatchDecision:
    action: Literal["dispatch"]
    worker_id: str
    instruction: str = field(repr=False)
    budget: int
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class BlockedDecision:
    action: Literal["blocked"]
    reason_code: Literal["DEPENDENCY_UNAVAILABLE", "NO_SAFE_ACTION"]


@dataclass(frozen=True, slots=True)
class RejectDecision:
    action: Literal["reject"]
    reason_code: Literal["TASK_UNSAFE", "TASK_UNSUPPORTED"]


@dataclass(frozen=True, slots=True)
class ControllerContext:
    assignment_id: str
    task_version: int
    task: str = field(repr=False)
    worker_id: str
    role: AgentRole
    project_id: str
    base_epoch: str
    knowledge_epoch: int
    allowed_operations: tuple[str, ...]
    max_budget: int
    max_timeout_seconds: float
    controller_version: int


class ControllerBackend(Protocol):
    @property
    def identity(self) -> BackendIdentity: ...

    async def preflight(self) -> None: ...

    async def plan(self, context: ControllerContext) -> ControllerDecision: ...


def controller_context_bytes(context: ControllerContext) -> bytes:
    """Serialize only model-visible data and descriptive operation labels."""
    return canonical_json(
        {
            "assignment_id": context.assignment_id,
            "task_version": context.task_version,
            "task": context.task,
            "worker_id": context.worker_id,
            "role": context.role.value,
            "project_id": context.project_id,
            "base_epoch": context.base_epoch,
            "knowledge_epoch": context.knowledge_epoch,
            "allowed_operations": _safe_operation_labels(context.allowed_operations),
            "max_budget": context.max_budget,
            "max_timeout_seconds": context.max_timeout_seconds,
        }
    )


def _safe_operation_labels(operations: tuple[str, ...]) -> tuple[str, ...]:
    if type(operations) is not tuple:
        raise ControllerBackendError("CONTROLLER_DECISION_INVALID")
    if any(type(operation) is not str for operation in operations):
        raise ControllerBackendError("CONTROLLER_DECISION_INVALID")
    if len(operations) > len(_SAFE_CONTROLLER_OPERATIONS):
        raise ControllerBackendError("CONTROLLER_DECISION_INVALID")
    if len(operations) != len(set(operations)):
        raise ControllerBackendError("CONTROLLER_DECISION_INVALID")
    if any(operation not in _SAFE_CONTROLLER_OPERATIONS for operation in operations):
        raise ControllerBackendError("CONTROLLER_DECISION_INVALID")
    return operations


def parse_controller_decision(raw: bytes, context: ControllerContext) -> ControllerDecision:
    """Parse a schema-valid, bounded controller decision without exposing raw output."""
    try:
        value: JsonValue = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControllerBackendError("CONTROLLER_DECISION_INVALID") from error
    try:
        _DECISION_VALIDATOR.validate(value)
    except ValidationError as error:
        raise ControllerBackendError("CONTROLLER_DECISION_INVALID") from error
    return _parse_valid_decision(value, context)


def _unique_json_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    value: dict[str, JsonValue] = {}
    for key, item in pairs:
        if key in value:
            raise ControllerBackendError("CONTROLLER_DECISION_INVALID")
        value[key] = item
    return value


def _parse_valid_decision(value: JsonValue, context: ControllerContext) -> ControllerDecision:
    if type(value) is not dict:
        raise ControllerBackendError("CONTROLLER_DECISION_INVALID")
    action = _text(value, "action")
    if action == "dispatch":
        return _dispatch_decision(value, context)
    if action == "blocked":
        return BlockedDecision("blocked", _blocked_reason(value))
    if action == "reject":
        return RejectDecision("reject", _reject_reason(value))
    raise ControllerBackendError("CONTROLLER_DECISION_INVALID")


def _dispatch_decision(value: dict[str, JsonValue], context: ControllerContext) -> DispatchDecision:
    worker_id = _text(value, "worker_id")
    if worker_id != context.worker_id:
        raise ControllerBackendError("CONTROLLER_WORKER_INVALID")
    instruction = _text(value, "instruction")
    try:
        instruction_size = len(instruction.encode())
    except UnicodeEncodeError as error:
        raise ControllerBackendError("CONTROLLER_INSTRUCTION_INVALID") from error
    if instruction_size > MAX_CONTROLLER_INSTRUCTION_BYTES:
        raise ControllerBackendError("CONTROLLER_INSTRUCTION_INVALID")
    budget = _integer(value, "budget")
    if budget > min(context.max_budget, MAX_WORKER_BUDGET):
        raise ControllerBackendError("CONTROLLER_BUDGET_INVALID")
    timeout_seconds = _finite_number(value, "timeout_seconds")
    if timeout_seconds > min(context.max_timeout_seconds, MAX_WORKER_TIMEOUT_SECONDS):
        raise ControllerBackendError("CONTROLLER_TIMEOUT_INVALID")
    return DispatchDecision("dispatch", worker_id, instruction, budget, timeout_seconds)


def _blocked_reason(
    value: dict[str, JsonValue],
) -> Literal["DEPENDENCY_UNAVAILABLE", "NO_SAFE_ACTION"]:
    reason_code = _text(value, "reason_code")
    if reason_code == "DEPENDENCY_UNAVAILABLE":
        return "DEPENDENCY_UNAVAILABLE"
    if reason_code == "NO_SAFE_ACTION":
        return "NO_SAFE_ACTION"
    raise ControllerBackendError("CONTROLLER_DECISION_INVALID")


def _reject_reason(value: dict[str, JsonValue]) -> Literal["TASK_UNSAFE", "TASK_UNSUPPORTED"]:
    reason_code = _text(value, "reason_code")
    if reason_code == "TASK_UNSAFE":
        return "TASK_UNSAFE"
    if reason_code == "TASK_UNSUPPORTED":
        return "TASK_UNSUPPORTED"
    raise ControllerBackendError("CONTROLLER_DECISION_INVALID")


def _text(value: dict[str, JsonValue], field_name: str) -> str:
    field_value = value.get(field_name)
    if type(field_value) is not str:
        raise ControllerBackendError("CONTROLLER_DECISION_INVALID")
    return field_value


def _integer(value: dict[str, JsonValue], field_name: str) -> int:
    field_value = value.get(field_name)
    if type(field_value) is not int:
        raise ControllerBackendError("CONTROLLER_DECISION_INVALID")
    return field_value


def _finite_number(value: dict[str, JsonValue], field_name: str) -> float:
    field_value = value.get(field_name)
    if type(field_value) is int:
        return float(field_value)
    if type(field_value) is float:
        if math.isfinite(field_value):
            return field_value
        raise ControllerBackendError("CONTROLLER_TIMEOUT_INVALID")
    raise ControllerBackendError("CONTROLLER_DECISION_INVALID")
