from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from aizim.domain import EpochPair, sha256_bytes
from aizim.domain.serialization import JsonValue
from aizim.state.events import utc_now


@dataclass(slots=True)
class DocumentBrokerError(RuntimeError):
    code: str

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    document_id: str
    display_name: PurePosixPath
    epoch_pair: EpochPair
    file_version: int
    content_hash: str
    content: bytes

    def __post_init__(self) -> None:
        valid = (
            type(self.document_id) is str
            and bool(self.document_id)
            and type(self.display_name) is PurePosixPath
            and not self.display_name.is_absolute()
            and bool(self.display_name.parts)
            and all(part != ".." and "\x00" not in part for part in self.display_name.parts)
            and type(self.epoch_pair) is EpochPair
            and type(self.file_version) is int
            and self.file_version >= 0
            and type(self.content_hash) is str
            and type(self.content) is bytes
            and re.fullmatch(r"[0-9a-f]{64}", self.content_hash) is not None
            and sha256_bytes(self.content) == self.content_hash
        )
        if not valid:
            raise DocumentBrokerError("INVALID_DOCUMENT_SNAPSHOT")


def _opaque_id() -> str:
    return secrets.token_hex(16)


@dataclass(frozen=True, slots=True)
class BrokerDependencies:
    clock: Callable[[], datetime] = utc_now
    lease_ids: Callable[[], str] = _opaque_id
    document_ids: Callable[[], str] = _opaque_id
    lease_lifetime: timedelta = field(default_factory=lambda: timedelta(minutes=15))

    def __post_init__(self) -> None:
        if (
            not callable(self.clock)
            or not callable(self.lease_ids)
            or not callable(self.document_ids)
            or type(self.lease_lifetime) is not timedelta
            or self.lease_lifetime <= timedelta(0)
        ):
            raise DocumentBrokerError("INVALID_BROKER_DEPENDENCIES")


class LeanRuntimeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _text(value: object) -> str:
    if type(value) is not str or not value:
        raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
    return value


@dataclass(frozen=True, slots=True)
class WorkerSession:
    run_id: str
    worker_id: str
    lease_id: str

    def __post_init__(self) -> None:
        for value in (self.run_id, self.worker_id, self.lease_id):
            _text(value)


@dataclass(frozen=True, slots=True)
class GoalHypothesis:
    name: str
    type: str

    def payload(self) -> dict[str, JsonValue]:
        return {"name": self.name, "type": self.type}


@dataclass(frozen=True, slots=True)
class Goal:
    hypotheses: tuple[GoalHypothesis, ...]
    target: str
    status: str
    pretty: str

    def payload(self) -> dict[str, JsonValue]:
        return {
            "hypotheses": [item.payload() for item in self.hypotheses],
            "target": self.target,
            "status": self.status,
            "pretty": self.pretty,
        }


@dataclass(frozen=True, slots=True)
class GoalResult:
    line_context: str
    goals: tuple[Goal, ...]
    goals_before: tuple[Goal, ...]
    goals_after: tuple[Goal, ...]
    status: str | None
    response_hash: str

    def payload(self) -> dict[str, JsonValue]:
        return {
            "line_context": self.line_context,
            "goals": [item.payload() for item in self.goals],
            "goals_before": [item.payload() for item in self.goals_before],
            "goals_after": [item.payload() for item in self.goals_after],
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class Diagnostic:
    severity: str
    message: str
    line: int
    column: int | None
    lean_tags: tuple[str, ...] | None

    def payload(self) -> dict[str, JsonValue]:
        return {
            "severity": self.severity,
            "message": self.message,
            "line": self.line,
            "column": self.column,
            "lean_tags": None if self.lean_tags is None else list(self.lean_tags),
        }


@dataclass(frozen=True, slots=True)
class AttemptResult:
    snippet: str
    diagnostics: tuple[Diagnostic, ...]
    timed_out: bool
    proof_status: str | None

    def payload(self) -> dict[str, JsonValue]:
        return {
            "snippet": self.snippet,
            "diagnostics": [item.payload() for item in self.diagnostics],
            "timed_out": self.timed_out,
            "proof_status": self.proof_status,
        }


@dataclass(frozen=True, slots=True)
class MultiAttemptResult:
    items: tuple[AttemptResult, ...]
    response_hash: str

    def payload(self) -> dict[str, JsonValue]:
        return {"items": [item.payload() for item in self.items]}


@dataclass(frozen=True, slots=True)
class DiagnosticsResult:
    partial: bool
    still_elaborating_lines: tuple[int, ...] | None
    success: bool
    timed_out: bool
    items: tuple[Diagnostic, ...]
    failed_dependencies: tuple[str, ...]
    response_hash: str

    def payload(self) -> dict[str, JsonValue]:
        return {
            "partial": self.partial,
            "still_elaborating_lines": (
                None if self.still_elaborating_lines is None else list(self.still_elaborating_lines)
            ),
            "success": self.success,
            "timed_out": self.timed_out,
            "items": [item.payload() for item in self.items],
            "failed_dependencies": list(self.failed_dependencies),
        }


@dataclass(frozen=True, slots=True)
class LeanProcess:
    pid: int
    parent_pid: int
    command: str
