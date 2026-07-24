from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from aizim.domain.serialization import JsonValue
from aizim.runtime.layout import ProjectLayout
from aizim.runtime.state_process import StateProcessError, validate_live_state_process
from aizim.state import StateService, StateServiceConfig
from aizim.state.control_operations import ControlOperationError
from aizim.state.operations import (
    RpcFailure,
    RpcProtocolError,
    RpcRequest,
    RpcResponse,
    RpcSuccess,
    dispatch_operation,
)
from aizim.state.projections import ProjectionRecord
from aizim.state.rpc import rpc_call


class StateClientError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectionDocument:
    entity_id: str
    projection_name: str
    state: dict[str, JsonValue]
    version: int


type ControlOperationName = Literal[
    "control.configure_controller",
    "control.register_worker",
    "control.assign_task",
]
type FileIdentity = tuple[int, int, int]


def _record_document(record: ProjectionRecord) -> ProjectionDocument:
    state: JsonValue = json.loads(record.state_json)
    if type(state) is not dict:
        raise StateClientError("projection state is malformed")
    return ProjectionDocument(record.entity_id, record.projection_name, state, record.version)


def _rpc_document(value: JsonValue) -> ProjectionDocument:
    if type(value) is not dict:
        raise StateClientError("projection response is malformed")
    entity_id = value.get("entity_id")
    name = value.get("projection_name")
    state = value.get("state")
    version = value.get("version")
    if type(entity_id) is not str or type(name) is not str or type(state) is not dict:
        raise StateClientError("projection response is malformed")
    if type(version) is not int:
        raise StateClientError("projection response is malformed")
    return ProjectionDocument(entity_id, name, state, version)


async def _rpc_projections(layout: ProjectLayout) -> tuple[ProjectionDocument, ...]:
    response = await rpc_call(
        layout.run_root / "state.sock", RpcRequest("query_projections", {}, None)
    )
    if not isinstance(response, RpcSuccess) or type(response.result) is not list:
        raise StateClientError("state projection query failed")
    return tuple(_rpc_document(item) for item in response.result)


async def _rpc_health(layout: ProjectLayout) -> JsonValue:
    response = await rpc_call(layout.run_root / "state.sock", RpcRequest("health", {}, None))
    if not isinstance(response, RpcSuccess):
        raise StateClientError("state health query failed")
    return response.result


async def _rpc_control(
    layout: ProjectLayout,
    operation: ControlOperationName,
    params: dict[str, JsonValue],
) -> RpcResponse:
    return await rpc_call(
        layout.run_root / "state.sock",
        RpcRequest(operation=operation, params=params, session_id=None),
    )


def _identity(path: Path) -> FileIdentity | None:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return None
    return status.st_dev, status.st_ino, status.st_ctime_ns


def _control_version(response: RpcResponse) -> int:
    if isinstance(response, RpcFailure):
        raise ControlOperationError(response.error.code)
    result = response.result
    if type(result) is not dict or result.keys() != {"version"}:
        raise StateClientError("control response is malformed")
    version = result["version"]
    if type(version) is not int or version <= 0:
        raise StateClientError("control response is malformed")
    return version


def call_control_operation(
    layout: ProjectLayout,
    operation: ControlOperationName,
    params: dict[str, JsonValue],
) -> int:
    pid_path = layout.run_root / "state.pid"
    socket_path = layout.run_root / "state.sock"
    before = (_identity(pid_path), _identity(socket_path))
    if before == (None, None):
        with StateService(
            StateServiceConfig(layout.root, "short-lived-control")
        ) as state:
            response = dispatch_operation(
                state,
                RpcRequest(operation=operation, params=params, session_id=None),
                False,
            )
        return _control_version(response)
    if None in before:
        raise StateClientError("live state ownership record is incomplete")
    try:
        validate_live_state_process(pid_path, socket_path)
        response = asyncio.run(_rpc_control(layout, operation, params))
        validate_live_state_process(pid_path, socket_path)
    except (EOFError, OSError, RpcProtocolError, StateProcessError) as error:
        raise StateClientError("live state service is unavailable") from error
    if before != (_identity(pid_path), _identity(socket_path)):
        raise StateClientError("live state ownership changed during control request")
    return _control_version(response)


def load_projections(layout: ProjectLayout) -> tuple[ProjectionDocument, ...]:
    if (layout.run_root / "state.sock").exists():
        try:
            validate_live_state_process(
                layout.run_root / "state.pid", layout.run_root / "state.sock"
            )
            if asyncio.run(_rpc_health(layout)) != {
                "event_schema_version": 1,
                "ready": True,
            }:
                raise StateClientError("live state schema is incompatible")
            records = asyncio.run(_rpc_projections(layout))
            validate_live_state_process(
                layout.run_root / "state.pid", layout.run_root / "state.sock"
            )
            return records
        except (StateProcessError, OSError, EOFError) as error:
            raise StateClientError("live state service is unavailable") from error
    if not layout.database_path.is_file():
        raise StateClientError("Aizim state is not initialized")
    try:
        with StateService(StateServiceConfig(layout.root, "status-session")) as state:
            return tuple(_record_document(record) for record in state.projections())
    except Exception as error:
        raise StateClientError("Aizim state is unavailable") from error


def check_state_health(layout: ProjectLayout) -> None:
    if (layout.run_root / "state.sock").exists():
        try:
            validate_live_state_process(
                layout.run_root / "state.pid", layout.run_root / "state.sock"
            )
            response = asyncio.run(_rpc_health(layout))
            if response != {"event_schema_version": 1, "ready": True}:
                raise StateClientError("live state schema is incompatible")
            validate_live_state_process(
                layout.run_root / "state.pid", layout.run_root / "state.sock"
            )
            return
        except (StateProcessError, OSError, EOFError) as error:
            raise StateClientError("live state service is unavailable") from error
    if not layout.database_path.is_file():
        raise StateClientError("Aizim state is not initialized")
    try:
        with StateService(StateServiceConfig(layout.root, "doctor-session")) as state:
            if state.health().event_schema_version != 1:
                raise StateClientError("state schema is incompatible")
    except Exception as error:
        if isinstance(error, StateClientError):
            raise
        raise StateClientError("Aizim state is unavailable") from error
