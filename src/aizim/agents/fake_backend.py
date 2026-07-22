from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from aizim.domain import AgentRole, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.gateway import GatewayFailure, GatewaySuccess, GatewayTool

from .backend import AgentRequest, AgentResult, BackendIdentity


class _GatewayTransport(Protocol):
    role: AgentRole

    async def call(
        self, operation: GatewayTool | str, payload: dict[str, JsonValue]
    ) -> GatewaySuccess | GatewayFailure: ...

    async def aclose(self) -> None: ...


type GatewayConnector = Callable[[Path, str], Awaitable[_GatewayTransport]]


@dataclass(frozen=True, slots=True)
class FakeToolAction:
    operation: GatewayTool
    payload: dict[str, JsonValue]


class FakeFixtureError(ValueError):
    pass


class FakeAgentBackend:
    def __init__(
        self,
        actions: tuple[FakeToolAction, ...],
        *,
        connect: GatewayConnector,
        cursor: Mapping[str, JsonValue] | None = None,
        known_delta: Mapping[str, JsonValue] | None = None,
    ) -> None:
        self._actions = actions
        self._connect = connect
        self._cursor = {} if cursor is None else dict(cursor)
        self._known_delta = None if known_delta is None else dict(known_delta)
        self._resolved_actions: tuple[FakeToolAction, ...] = ()

    @classmethod
    def from_fixture(
        cls,
        fixture: Path,
        *,
        connect: GatewayConnector,
        cursor: Mapping[str, JsonValue],
        round_index: int = 0,
        known_delta: Mapping[str, JsonValue] | None = None,
    ) -> FakeAgentBackend:
        return cls(
            _fixture_actions(fixture, round_index),
            connect=connect,
            cursor=cursor,
            known_delta=known_delta,
        )

    @property
    def identity(self) -> BackendIdentity:
        return BackendIdentity("fake", "deterministic-v1", None)

    @property
    def resolved_actions(self) -> tuple[FakeToolAction, ...]:
        return self._resolved_actions

    async def run(self, request: AgentRequest) -> AgentResult:
        transport = await self._connect(request.gateway_broker_socket, request.gateway_session_id)
        documents: list[JsonValue] = []
        resolved_actions: list[FakeToolAction] = []
        delta = self._known_delta
        cursor = {**self._cursor, **request.context}
        status = "submitted"
        try:
            if transport.role is not request.role:
                status = "failed"
            else:
                for action in self._actions:
                    resolved = _resolve_action(action, cursor, delta)
                    result = await transport.call(resolved.operation, resolved.payload)
                    documents.append(_result_document(result))
                    resolved_actions.append(resolved)
                    delta = _published_delta(result) or delta
                    if isinstance(result, GatewayFailure):
                        status = "failed"
                        break
        finally:
            await transport.aclose()
        self._resolved_actions = tuple(resolved_actions)
        summary = "fixture complete" if status == "submitted" else "gateway action failed"
        final = {"status": status, "summary": summary}
        return AgentResult(
            worker_id=request.worker_id,
            status=status,
            summary=summary,
            transport_event_hash=sha256_json(documents),
            final_message_hash=sha256_json(final),
            exit_code=0 if status == "submitted" else 4,
        )


def _result_document(result: GatewaySuccess | GatewayFailure) -> JsonValue:
    match result:
        case GatewaySuccess(result=value):
            return {"ok": True, "result": value}
        case GatewayFailure(error=error):
            return {
                "ok": False,
                "error": {"code": error.code, "message": error.message},
            }


def _fixture_actions(path: Path, round_index: int) -> tuple[FakeToolAction, ...]:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FakeFixtureError("INVALID_FAKE_FIXTURE") from error
    if type(value) is not dict or set(value) not in ({"actions"}, {"rounds"}):
        raise FakeFixtureError("INVALID_FAKE_FIXTURE")
    record = cast(dict[str, object], value)
    actions = record.get("actions")
    if actions is None:
        rounds = record["rounds"]
        if type(round_index) is not int or round_index < 0 or type(rounds) is not list:
            raise FakeFixtureError("INVALID_FAKE_FIXTURE")
        if round_index >= len(rounds) or type(rounds[round_index]) is not list:
            raise FakeFixtureError("INVALID_FAKE_FIXTURE")
        actions = rounds[round_index]
    if type(actions) is not list:
        raise FakeFixtureError("INVALID_FAKE_FIXTURE")
    return tuple(_fixture_action(item) for item in actions)


def _fixture_action(value: object) -> FakeToolAction:
    if type(value) is not dict or set(value) != {"operation", "payload"}:
        raise FakeFixtureError("INVALID_FAKE_FIXTURE")
    record = cast(dict[str, object], value)
    operation, payload = record["operation"], record["payload"]
    if type(operation) is not str or type(payload) is not dict:
        raise FakeFixtureError("INVALID_FAKE_FIXTURE")
    try:
        tool = GatewayTool(operation)
    except ValueError as error:
        raise FakeFixtureError("INVALID_FAKE_FIXTURE") from error
    return FakeToolAction(tool, cast(dict[str, JsonValue], payload))


def _resolve_action(
    action: FakeToolAction,
    cursor: Mapping[str, JsonValue],
    delta: dict[str, JsonValue] | None,
) -> FakeToolAction:
    payload = cast(dict[str, JsonValue], _resolve_value(action.payload, cursor))
    reference = payload.pop("delta_reference", None)
    if reference is None:
        return FakeToolAction(action.operation, payload)
    if delta is None or "accepted" in payload or "import_module" in payload:
        raise FakeFixtureError("DELTA_REFERENCE_UNAVAILABLE")
    template = _reference_template(reference)
    name, module = _delta_text(delta, "fully_qualified_name"), _delta_text(delta, "module")
    payload["accepted"] = template.replace("{fully_qualified_name}", name)
    payload["import_module"] = module
    return FakeToolAction(action.operation, payload)


def _resolve_value(value: JsonValue, cursor: Mapping[str, JsonValue]) -> JsonValue:
    if type(value) is list:
        return [_resolve_value(item, cursor) for item in value]
    if type(value) is not dict:
        return value
    if set(value) == {"$cursor"}:
        key = value["$cursor"]
        if type(key) is not str or key not in cursor:
            raise FakeFixtureError("UNKNOWN_CURSOR_FIELD")
        return cursor[key]
    return {key: _resolve_value(item, cursor) for key, item in value.items()}


def _reference_template(value: JsonValue) -> str:
    if type(value) is not dict or set(value) != {"template"}:
        raise FakeFixtureError("INVALID_DELTA_REFERENCE")
    template = value["template"]
    if type(template) is not str or template.count("{fully_qualified_name}") != 1:
        raise FakeFixtureError("INVALID_DELTA_REFERENCE")
    return template


def _published_delta(result: GatewaySuccess | GatewayFailure) -> dict[str, JsonValue] | None:
    if not isinstance(result, GatewaySuccess) or type(result.result) is not dict:
        return None
    deltas = result.result.get("deltas")
    if type(deltas) is not list or not deltas or type(deltas[-1]) is not dict:
        return None
    return deltas[-1]


def _delta_text(delta: Mapping[str, JsonValue], field: str) -> str:
    value = delta.get(field)
    if type(value) is not str or not value:
        raise FakeFixtureError("INVALID_DELTA_REFERENCE")
    return value
