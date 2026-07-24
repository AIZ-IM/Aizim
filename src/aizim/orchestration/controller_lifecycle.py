from __future__ import annotations

import json

from aizim.domain.serialization import JsonValue
from aizim.state import AppendEventCommand, ProjectionRecord, StateService

from .control_plane import ControllerProvider
from .controller_execution import ControllerExecutionError

_CONTROLLER_ID = "primary"


def start_controller(
    state: StateService,
    *,
    session_id: str,
    controller_version: int,
    provider: ControllerProvider,
    backend_version: str,
    executable_hash: str,
) -> None:
    configured = state.query_projection("controller", _CONTROLLER_ID)
    if configured is None:
        raise ControllerExecutionError("CONTROLLER_NOT_CONFIGURED")
    configured_payload = _payload(configured)
    if configured.version != controller_version:
        raise ControllerExecutionError("CONTROLLER_VERSION_STALE")
    if type(provider) is not ControllerProvider:
        raise ControllerExecutionError("CONTROLLER_PROVIDER_INVALID")
    if configured_payload.get("provider") != provider.value:
        raise ControllerExecutionError("CONTROLLER_PROVIDER_STALE")
    state.append_event(
        AppendEventCommand(
            "ControllerStarted",
            _CONTROLLER_ID,
            None,
            None,
            {
                "controller_id": _CONTROLLER_ID,
                "controller_session_id": session_id,
                "controller_version": controller_version,
                "provider": provider.value,
                "backend_version": backend_version,
                "executable_hash": executable_hash,
            },
        )
    )


def recover_unclean_controller(state: StateService) -> str | None:
    runtime = state.query_projection("controller_runtime", _CONTROLLER_ID)
    if runtime is None:
        return None
    payload = _payload(runtime)
    if payload.get("status") != "running":
        return None
    session_id = _text(payload, "controller_session_id")
    state.append_event(
        AppendEventCommand(
            "ControllerCrashed",
            _CONTROLLER_ID,
            None,
            None,
            {
                "controller_id": _CONTROLLER_ID,
                "controller_session_id": session_id,
                "reason_code": "UNCLEAN_SHUTDOWN",
            },
        )
    )
    return session_id


def stop_controller(state: StateService, session_id: str) -> None:
    runtime = state.query_projection("controller_runtime", _CONTROLLER_ID)
    if runtime is None or _payload(runtime).get("status") != "running":
        raise ControllerExecutionError("CONTROLLER_NOT_RUNNING")
    if _text(_payload(runtime), "controller_session_id") != session_id:
        raise ControllerExecutionError("CONTROLLER_SESSION_STALE")
    state.append_event(
        AppendEventCommand(
            "ControllerStopped",
            _CONTROLLER_ID,
            None,
            None,
            {
                "controller_id": _CONTROLLER_ID,
                "controller_session_id": session_id,
                "reason_code": "OPERATOR_SIGNAL",
            },
        )
    )


def _payload(record: ProjectionRecord) -> dict[str, JsonValue]:
    state: JsonValue = json.loads(record.state_json)
    payload = state.get("payload") if type(state) is dict else None
    if type(payload) is not dict:
        raise ControllerExecutionError("CONTROLLER_STATE_INVALID")
    return payload


def _text(payload: dict[str, JsonValue], field: str) -> str:
    value = payload.get(field)
    if type(value) is not str or not value:
        raise ControllerExecutionError("CONTROLLER_STATE_INVALID")
    return value
