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


def _optional_record(
    value: object, required: frozenset[str], optional: frozenset[str]
) -> dict[str, object]:
    if (
        type(value) is not dict
        or not required <= value.keys()
        or not value.keys() <= required | optional
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
        None if payload["goal"] is None else _string(payload["goal"]),
        _string(payload["status"]),
        _string(payload["pretty"]),
    )


def _diagnostic(value: object) -> Diagnostic:
    payload = _optional_record(
        value,
        frozenset({"severity", "message", "line", "column", "category"}),
        frozenset({"lean_tags", "hint"}),
    )
    if payload["category"] not in ("diagnostic", "linter", "sorry", "suggestion"):
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    if "hint" in payload:
        _string(payload["hint"])
    line, column, tags = payload["line"], payload["column"], payload.get("lean_tags")
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
    payload = _optional_record(
        value, frozenset({"line_context"}),
        frozenset({"goals", "goals_before", "goals_after", "status"}),
    )
    status = payload.get("status")
    if status is not None:
        status = _string(status)
    return GoalResult(
        _string(payload["line_context"]),
        _goals(payload.get("goals")),
        _goals(payload.get("goals_before")),
        _goals(payload.get("goals_after")),
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
        item = _optional_record(
            raw, frozenset({"snippet", "goals", "diagnostics", "timed_out"}),
            frozenset({"proof_status"}),
        )
        diagnostics, status = item["diagnostics"], item.get("proof_status")
        if (
            type(item["goals"]) is not list
            or type(diagnostics) is not list
            or type(item["timed_out"]) is not bool
        ):
            raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
        goals = tuple(_string(goal) for goal in item["goals"])
        if status is not None:
            status = _string(status)
        items.append(
            AttemptResult(
                _string(item["snippet"]),
                tuple(_diagnostic(value) for value in diagnostics),
                item["timed_out"],
                status,
                goals,
            )
        )
    return MultiAttemptResult(tuple(items), response_hash)


def parse_hover_info(value: object) -> str:
    payload = _record(value, frozenset({"symbol", "info", "diagnostics"}))
    diagnostics = payload["diagnostics"]
    if type(diagnostics) is not list:
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    _ = _string(payload["symbol"]), tuple(_diagnostic(item) for item in diagnostics)
    return _string(payload["info"])


def parse_diagnostics(value: object, response_hash: str) -> DiagnosticsResult:
    payload = _optional_record(
        value, frozenset({"partial", "success", "timed_out", "items"}),
        frozenset({"still_elaborating_lines", "failed_dependencies"}),
    )
    pending = payload.get("still_elaborating_lines")
    items = payload["items"]
    failed = payload.get("failed_dependencies", [])
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
        ranges: list[tuple[int, int]] = []
        for item in pending:
            if type(item) is not list or len(item) != 2:
                raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
            start, end = item
            if type(start) is not int or type(end) is not int or not 1 <= start <= end:
                raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
            ranges.append((start, end))
        parsed_pending = tuple(ranges)
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
