from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from aizim.domain.serialization import JsonValue
from aizim.gateway.capabilities import AuthorizedCall, GatewayTool

from .models import (
    DiagnosticsResult,
    GoalResult,
    LeanRuntimeError,
    MultiAttemptResult,
    WorkerSession,
)


class _WorkerRuntime(Protocol):
    async def goal(
        self, session: WorkerSession, document_id: str, line: int, column: int | None = None
    ) -> GoalResult: ...

    async def multi_attempt(
        self,
        session: WorkerSession,
        document_id: str,
        line: int,
        snippets: tuple[str, ...],
        column: int | None = None,
    ) -> MultiAttemptResult: ...

    async def diagnostics(
        self,
        session: WorkerSession,
        document_id: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> DiagnosticsResult: ...


def targets(
    runtime: _WorkerRuntime,
) -> Mapping[GatewayTool, Callable[[AuthorizedCall], Awaitable[JsonValue]]]:
    return {
        GatewayTool.LEAN_GOAL: lambda call: goal(runtime, call),
        GatewayTool.LEAN_MULTI_ATTEMPT: lambda call: multi_attempt(runtime, call),
        GatewayTool.LEAN_DIAGNOSTICS: lambda call: diagnostics(runtime, call),
    }


async def goal(runtime: _WorkerRuntime, call: AuthorizedCall) -> JsonValue:
    session = _session(call)
    document_id, line = _text(call.payload, "document_id"), _line(call.payload, "line")
    result = await runtime.goal(session, document_id, line, _optional_line(call.payload, "column"))
    return result.payload()


async def multi_attempt(runtime: _WorkerRuntime, call: AuthorizedCall) -> JsonValue:
    session = _session(call)
    document_id, line = _text(call.payload, "document_id"), _line(call.payload, "line")
    result = await runtime.multi_attempt(
        session,
        document_id,
        line,
        _snippets(call.payload),
        _optional_line(call.payload, "column"),
    )
    return result.payload()


async def diagnostics(runtime: _WorkerRuntime, call: AuthorizedCall) -> JsonValue:
    session = _session(call)
    document_id = _text(call.payload, "document_id")
    result = await runtime.diagnostics(
        session,
        document_id,
        _optional_line(call.payload, "start_line"),
        _optional_line(call.payload, "end_line"),
    )
    return result.payload()


def _session(call: AuthorizedCall) -> WorkerSession:
    if call.lease_id is None:
        raise LeanRuntimeError("LEASE_REQUIRED")
    return WorkerSession(call.run_id, call.worker_id, call.lease_id)


def _text(payload: dict[str, JsonValue], key: str) -> str:
    value = payload.get(key)
    if type(value) is not str or not value:
        raise LeanRuntimeError("INVALID_LEAN_REQUEST")
    return value


def _line(payload: dict[str, JsonValue], key: str) -> int:
    value = payload.get(key)
    if type(value) is not int or value < 1:
        raise LeanRuntimeError("INVALID_LEAN_REQUEST")
    return value


def _optional_line(payload: dict[str, JsonValue], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise LeanRuntimeError("INVALID_LEAN_REQUEST")
    return value


def _snippets(payload: dict[str, JsonValue]) -> tuple[str, ...]:
    value = payload.get("snippets")
    if type(value) is not list or not value:
        raise LeanRuntimeError("INVALID_LEAN_REQUEST")
    snippets: list[str] = []
    for item in value:
        if type(item) is not str or not item:
            raise LeanRuntimeError("INVALID_LEAN_REQUEST")
        snippets.append(item)
    return tuple(snippets)
