from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from aizim.domain.serialization import JsonValue
from aizim.runtime.layout import ProjectLayout
from aizim.state import StateService, StateServiceConfig
from aizim.state.operations import RpcRequest, RpcSuccess
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


async def _rpc_health(layout: ProjectLayout) -> None:
    response = await rpc_call(layout.run_root / "state.sock", RpcRequest("health", {}, None))
    if not isinstance(response, RpcSuccess):
        raise StateClientError("state health query failed")


def load_projections(layout: ProjectLayout) -> tuple[ProjectionDocument, ...]:
    if (layout.run_root / "state.sock").exists():
        try:
            return asyncio.run(_rpc_projections(layout))
        except (OSError, EOFError) as error:
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
            asyncio.run(_rpc_health(layout))
            return
        except (OSError, EOFError) as error:
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
