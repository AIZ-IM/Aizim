from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from aizim.domain.serialization import JsonValue

from .event_payload import FrozenJsonObject, freeze_payload
from .events import EventValidationError, event_as_dict
from .projections import ProjectionRecord
from .store_contracts import (
    DuplicateEventError,
    EventRecord,
    ProjectionAuthorityError,
    ReplayVerification,
    StoreHealth,
)


@dataclass(frozen=True, slots=True)
class RpcProtocolError(ValueError):
    code: str

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class RpcRequest:
    operation: str
    params: dict[str, JsonValue]
    session_id: str | None


@dataclass(frozen=True, slots=True)
class RpcErrorBody:
    code: str
    message: str
    event_id: str | None


@dataclass(frozen=True, slots=True)
class RpcSuccess:
    ok: Literal[True]
    result: JsonValue

    def __init__(self, result: JsonValue) -> None:
        object.__setattr__(self, "ok", True)
        object.__setattr__(self, "result", result)


@dataclass(frozen=True, slots=True)
class RpcFailure:
    ok: Literal[False]
    error: RpcErrorBody

    def __init__(self, error: RpcErrorBody) -> None:
        object.__setattr__(self, "ok", False)
        object.__setattr__(self, "error", error)


type RpcResponse = RpcSuccess | RpcFailure


def rpc_failure(code: str, message: str, event_id: str | None = None) -> RpcFailure:
    return RpcFailure(RpcErrorBody(code=code, message=message, event_id=event_id))


@dataclass(frozen=True, slots=True, init=False)
class AppendEventCommand:
    event_type: str
    actor: str
    run_id: str | None
    causation_id: str | None
    payload: FrozenJsonObject

    def __init__(
        self,
        event_type: str,
        actor: str,
        run_id: str | None,
        causation_id: str | None,
        payload: dict[str, JsonValue],
    ) -> None:
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "causation_id", causation_id)
        object.__setattr__(self, "payload", freeze_payload(payload))


class StateOperations(Protocol):
    def append_event(self, command: AppendEventCommand) -> EventRecord: ...

    def health(self) -> StoreHealth: ...

    def query_projection(self, name: str, entity_id: str) -> ProjectionRecord | None: ...

    def query_events(self, run_id: str | None = None) -> tuple[EventRecord, ...]: ...

    def projections(self, name: str | None = None) -> tuple[ProjectionRecord, ...]: ...

    def logical_digest(self) -> str: ...

    def replay_verify(self) -> ReplayVerification: ...


def _text(params: dict[str, JsonValue], key: str) -> str:
    value = params.get(key)
    if type(value) is not str or not value:
        raise RpcProtocolError("INVALID_PARAMS")
    return value


def _optional_text(params: dict[str, JsonValue], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if type(value) is not str or not value:
        raise RpcProtocolError("INVALID_PARAMS")
    return value


def _keys(
    params: dict[str, JsonValue], required: frozenset[str], optional: frozenset[str] = frozenset()
) -> None:
    if not required <= params.keys() or params.keys() - required - optional:
        raise RpcProtocolError("INVALID_PARAMS")


def _append_command(params: dict[str, JsonValue]) -> AppendEventCommand:
    _keys(
        params,
        frozenset({"event_type", "actor", "run_id", "causation_id", "payload"}),
    )
    payload = params["payload"]
    if type(payload) is not dict:
        raise RpcProtocolError("INVALID_PARAMS")
    return AppendEventCommand(
        event_type=_text(params, "event_type"),
        actor=_text(params, "actor"),
        run_id=_optional_text(params, "run_id"),
        causation_id=_optional_text(params, "causation_id"),
        payload=payload,
    )


def _projection_result(record: ProjectionRecord | None) -> JsonValue:
    if record is None:
        return None
    state: JsonValue = json.loads(record.state_json)
    return {
        "entity_id": record.entity_id,
        "projection_name": record.projection_name,
        "state": state,
        "version": record.version,
    }


def _projection_document(record: ProjectionRecord) -> dict[str, JsonValue]:
    state: JsonValue = json.loads(record.state_json)
    return {
        "entity_id": record.entity_id,
        "projection_name": record.projection_name,
        "state": state,
        "version": record.version,
    }


def _health(target: StateOperations, request: RpcRequest, trusted: bool) -> RpcResponse:
    del trusted
    _keys(request.params, frozenset())
    return RpcSuccess({"event_schema_version": target.health().event_schema_version, "ready": True})


def _append(target: StateOperations, request: RpcRequest, trusted: bool) -> RpcResponse:
    if not trusted:
        return rpc_failure("NOT_AUTHORIZED", "trusted service session is required")
    record = target.append_event(_append_command(request.params))
    return RpcSuccess({"event_id": record.envelope.event_id, "sequence": record.sequence})


def _projection(target: StateOperations, request: RpcRequest, trusted: bool) -> RpcResponse:
    del trusted
    _keys(request.params, frozenset({"projection_name", "entity_id"}))
    record = target.query_projection(
        _text(request.params, "projection_name"), _text(request.params, "entity_id")
    )
    return RpcSuccess(_projection_result(record))


def _events(target: StateOperations, request: RpcRequest, trusted: bool) -> RpcResponse:
    del trusted
    _keys(request.params, frozenset(), frozenset({"run_id"}))
    records = target.query_events(_optional_text(request.params, "run_id"))
    return RpcSuccess(
        [{"sequence": record.sequence, **event_as_dict(record.envelope)} for record in records]
    )


def _projections(target: StateOperations, request: RpcRequest, trusted: bool) -> RpcResponse:
    del trusted
    _keys(request.params, frozenset(), frozenset({"projection_name"}))
    records = target.projections(_optional_text(request.params, "projection_name"))
    return RpcSuccess([_projection_document(record) for record in records])


def _digest(target: StateOperations, request: RpcRequest, trusted: bool) -> RpcResponse:
    del trusted
    _keys(request.params, frozenset())
    return RpcSuccess({"logical_digest": target.logical_digest()})


def _replay(target: StateOperations, request: RpcRequest, trusted: bool) -> RpcResponse:
    del trusted
    _keys(request.params, frozenset())
    result = target.replay_verify()
    return RpcSuccess({"logical_digest": result.logical_digest, "matched": result.matched})


def _control(target: StateOperations, request: RpcRequest, trusted: bool) -> RpcResponse:
    from aizim.domain import AgentRole

    from .control_operations import (
        ControlOperationError,
        assign_task,
        configure_controller,
        register_worker,
    )

    del trusted
    try:
        match request.operation:
            case "control.configure_controller":
                _keys(request.params, frozenset({"provider"}), frozenset({"model"}))
                version = configure_controller(
                    target,
                    _text(request.params, "provider"),
                    _optional_text(request.params, "model"),
                )
            case "control.register_worker":
                _keys(request.params, frozenset({"worker_id", "role"}))
                role_text = _text(request.params, "role")
                try:
                    role = AgentRole(role_text)
                except ValueError:
                    raise ControlOperationError("WORKER_ROLE_INVALID") from None
                version = register_worker(target, _text(request.params, "worker_id"), role)
            case "control.assign_task":
                _keys(request.params, frozenset({"worker_id", "task"}))
                version = assign_task(
                    target,
                    _text(request.params, "worker_id"),
                    _text(request.params, "task"),
                )
            case _:
                raise RpcProtocolError("INVALID_PARAMS")
    except ControlOperationError as error:
        messages = {
            "CONTROLLER_NOT_CONFIGURED": "controller is not configured",
            "CONTROLLER_PROVIDER_INVALID": "controller provider is invalid",
            "CONTROLLER_PROVIDER_UNSUPPORTED": "controller provider is unsupported",
            "CONTROLLER_MODEL_INVALID": "controller model is invalid",
            "WORKER_ALREADY_REGISTERED": "worker is already registered",
            "WORKER_NOT_REGISTERED": "worker is not registered",
            "WORKER_ROLE_INVALID": "worker role is invalid",
            "WORKER_ID_INVALID": "worker id is invalid",
            "WORKER_TASK_INVALID": "worker task is invalid",
            "CONTROL_STATE_INVALID": "control state is invalid",
        }
        return rpc_failure(error.code, messages.get(error.code, "control request failed"))
    return RpcSuccess({"version": version})


type OperationHandler = Callable[[StateOperations, RpcRequest, bool], RpcResponse]


def _research(target: StateOperations, request: RpcRequest, trusted: bool) -> RpcResponse:
    from .research_operations import dispatch_research

    del trusted
    return dispatch_research(target, request)


_HANDLERS: Final[dict[str, OperationHandler]] = {
    "control.research": _research,
    "health": _health,
    "append_event": _append,
    "query_projection": _projection,
    "query_events": _events,
    "query_projections": _projections,
    "logical_digest": _digest,
    "replay_verify": _replay,
}
_HANDLERS.update(
    {
        "control.configure_controller": _control,
        "control.register_worker": _control,
        "control.assign_task": _control,
    }
)


def dispatch_operation(target: StateOperations, request: RpcRequest, trusted: bool) -> RpcResponse:
    handler = _HANDLERS.get(request.operation)
    if handler is None:
        return rpc_failure("UNKNOWN_OPERATION", "operation is not available")
    try:
        return handler(target, request, trusted)
    except RpcProtocolError:
        return rpc_failure("INVALID_PARAMS", "request parameters are invalid")
    except EventValidationError:
        return rpc_failure("INVALID_EVENT", "event request is invalid")
    except DuplicateEventError as error:
        return rpc_failure("DUPLICATE_EVENT", "event id already exists", error.event_id)
    except ProjectionAuthorityError:
        return rpc_failure("INVALID_PROJECTION", "projection is not available")
