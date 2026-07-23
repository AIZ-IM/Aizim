from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from .models import LeanRuntimeError


def _object(value: object, fields: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    if any(type(key) is not str for key in value):
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    return cast(dict[str, object], value)


@dataclass(frozen=True, slots=True)
class BuildResult:
    success: bool
    output: str
    errors: tuple[str, ...]
    response_hash: str


@dataclass(frozen=True, slots=True)
class VerificationResult:
    axioms: tuple[str, ...]
    warnings: tuple[str, ...]
    response_hash: str


def parse_build(value: object, response_hash: str) -> BuildResult:
    payload = _object(value, frozenset({"success", "output", "errors"}))
    errors = payload["errors"]
    if type(payload["success"]) is not bool or type(payload["output"]) is not str:
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    if type(errors) is not list:
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    return BuildResult(
        payload["success"],
        payload["output"],
        tuple(_string(item) for item in errors),
        response_hash,
    )


def parse_verification(value: object, response_hash: str) -> VerificationResult:
    payload = _object(value, frozenset({"axioms", "warnings"}))
    axioms, warnings = payload["axioms"], payload["warnings"]
    if type(axioms) is not list or type(warnings) is not list:
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    return VerificationResult(
        tuple(_string(item) for item in axioms),
        tuple(_source_warning(item) for item in warnings),
        response_hash,
    )


def _string(value: object) -> str:
    if type(value) is not str:
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    return value


def _source_warning(value: object) -> str:
    warning = _object(value, frozenset({"line", "pattern"}))
    line, pattern = warning["line"], warning["pattern"]
    if type(line) is not int or line < 0 or type(pattern) is not str or not pattern:
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    return f"line {line}: {pattern}"
