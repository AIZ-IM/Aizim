from __future__ import annotations

from typing import cast

from .models import (
    AttemptResult,
    Diagnostic,
    DiagnosticsResult,
    Goal,
    GoalHypothesis,
    GoalResult,
    LeanRuntimeError,
    MultiAttemptResult,
)


def _record(value: object, fields: frozenset[str]) -> dict[str, object]:
    if (
        type(value) is not dict
        or set(value) != fields
        or any(type(key) is not str for key in value)
    ):
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    return cast(dict[str, object], value)


def _string(value: object) -> str:
    if type(value) is not str:
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    return value


def _goals(value: object) -> tuple[Goal, ...]:
    if value is None:
        return ()
    if type(value) is not list:
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    return tuple(_goal(item) for item in value)


def _goal(value: object) -> Goal:
    payload = _record(value, frozenset({"context", "goal", "status", "pretty"}))
    context = payload["context"]
    if type(context) is not list:
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    hypotheses = []
    for item in context:
        entry = _record(item, frozenset({"name", "type"}))
        hypotheses.append(GoalHypothesis(_string(entry["name"]), _string(entry["type"])))
    return Goal(
        tuple(hypotheses),
        _string(payload["goal"]),
        _string(payload["status"]),
        _string(payload["pretty"]),
    )


def _diagnostic(value: object) -> Diagnostic:
    payload = _record(value, frozenset({"severity", "message", "line", "column", "lean_tags"}))
    line, column, tags = payload["line"], payload["column"], payload["lean_tags"]
    if type(line) is not int or (column is not None and type(column) is not int):
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    if tags is None:
        parsed_tags = None
    elif type(tags) is list:
        parsed_tags = tuple(_string(item) for item in tags)
    else:
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    return Diagnostic(
        _string(payload["severity"]),
        _string(payload["message"]),
        line,
        column,
        parsed_tags,
    )


def parse_goal(value: object, response_hash: str) -> GoalResult:
    fields = {"line_context", "goals", "goals_before", "goals_after", "status"}
    payload = _record(value, frozenset(fields))
    status = payload["status"]
    if status is not None:
        status = _string(status)
    return GoalResult(
        _string(payload["line_context"]),
        _goals(payload["goals"]),
        _goals(payload["goals_before"]),
        _goals(payload["goals_after"]),
        status,
        response_hash,
    )


def parse_attempts(value: object, response_hash: str) -> MultiAttemptResult:
    payload = _record(value, frozenset({"items"}))
    raw_items = payload["items"]
    if type(raw_items) is not list:
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    items = []
    for raw in raw_items:
        fields = {"snippet", "goals", "diagnostics", "timed_out", "proof_status"}
        item = _record(raw, frozenset(fields))
        diagnostics, status = item["diagnostics"], item["proof_status"]
        if (
            type(item["goals"]) is not list
            or type(diagnostics) is not list
            or type(item["timed_out"]) is not bool
        ):
            raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
        if status is not None:
            status = _string(status)
        items.append(
            AttemptResult(
                _string(item["snippet"]),
                tuple(_diagnostic(value) for value in diagnostics),
                item["timed_out"],
                status,
            )
        )
    return MultiAttemptResult(tuple(items), response_hash)


def parse_diagnostics(value: object, response_hash: str) -> DiagnosticsResult:
    fields = {
        "partial",
        "still_elaborating_lines",
        "success",
        "timed_out",
        "items",
        "failed_dependencies",
    }
    payload = _record(value, frozenset(fields))
    pending = payload["still_elaborating_lines"]
    items = payload["items"]
    failed = payload["failed_dependencies"]
    if (
        type(payload["partial"]) is not bool
        or type(payload["success"]) is not bool
        or type(payload["timed_out"]) is not bool
        or type(items) is not list
        or type(failed) is not list
    ):
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    if pending is None:
        parsed_pending = None
    elif type(pending) is list:
        if any(type(item) is not int for item in pending):
            raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
        parsed_pending = tuple(cast(int, item) for item in pending)
    else:
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    return DiagnosticsResult(
        payload["partial"],
        parsed_pending,
        payload["success"],
        payload["timed_out"],
        tuple(_diagnostic(item) for item in items),
        tuple(_string(item) for item in failed),
        response_hash,
    )
