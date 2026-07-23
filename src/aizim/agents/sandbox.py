from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from aizim.domain.serialization import JsonValue
from aizim.state.operations import AppendEventCommand
from aizim.state.store_contracts import EventRecord

GATE_COMPLETION_OPERATION = "gate_b_complete"


class ProbeOperation(StrEnum):
    READ_STATE_DATABASE = "read_state_database"
    WRITE_STATE_DATABASE = "write_state_database"
    WRITE_UNLEASED_FILE = "write_unleased_file"
    WRITE_SHARED_ARTIFACT = "write_shared_artifact"
    TRAVERSE_TO_UNLEASED_FILE = "traverse_to_unleased_file"
    SCRATCH_SYMLINK_TO_STATE = "scratch_symlink_to_state"
    CONNECT_GATEWAY_UNIX_SOCKET = "connect_gateway_unix_socket"
    CONNECT_NONALLOWLISTED_TCP = "connect_nonallowlisted_tcp"
    READ_SECRET_ENVIRONMENT = "read_secret_environment"
    WRITE_ALLOWED_SCRATCH = "write_allowed_scratch"
    READ_ALLOWED_VIEW = "read_allowed_view"


type ProbeVerdict = Literal["denied", "allowed"]
type SandboxPlatform = Literal["darwin", "linux"]


class ProbeEventSink(Protocol):
    def append_event(self, command: AppendEventCommand) -> EventRecord: ...

    def logical_digest(self) -> str: ...


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    project_root: Path
    view_root: Path
    scratch_root: Path
    command: tuple[str, ...]
    parent_env: Mapping[str, str] = field(repr=False)
    runtime_read_roots: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class SandboxLaunchSpec:
    platform_id: SandboxPlatform
    argv: tuple[str, ...]
    cwd: Path
    parent_env: Mapping[str, str] = field(repr=False)
    shell_env: Mapping[str, str]
    view_root: Path
    scratch_root: Path
    profile_id: str
    policy_hash: str


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    probe_id: str
    run_id: str
    project_root: Path
    view_root: Path
    scratch_root: Path
    state_database: Path
    unleased_file: Path
    shared_artifact: Path
    gateway_socket: Path
    tcp_host: str
    tcp_port: int
    allowed_view_file: Path
    allowed_view_sha256: str
    secret_environment_name: str
    parent_env: Mapping[str, str] = field(repr=False)
    event_sink: ProbeEventSink = field(repr=False)
    timeout_seconds: float = 20.0
    runtime_read_roots: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class ProbeAttempt:
    operation: ProbeOperation
    verdict: ProbeVerdict
    reason_code: str


@dataclass(frozen=True, slots=True)
class ProbeReport:
    platform_id: SandboxPlatform
    passed: bool
    codex_version: str
    sandbox_executable: str
    policy_hash: str
    attempts: tuple[ProbeAttempt, ...]
    unexpected_allows: tuple[ProbeOperation, ...]
    logical_digest_before: str
    logical_digest_after: str


class SandboxAdapter(Protocol):
    @property
    def platform_id(self) -> SandboxPlatform: ...

    def compile(self, request: SandboxRequest) -> SandboxLaunchSpec: ...

    async def launch_probe(self, request: ProbeRequest) -> ProbeReport: ...


class LaunchRequest(Protocol):
    @property
    def view_root(self) -> Path: ...

    @property
    def scratch_root(self) -> Path: ...


def validate_launch_spec(request: LaunchRequest, spec: SandboxLaunchSpec) -> None:
    digest = spec.policy_hash
    command = request.command if isinstance(request, SandboxRequest) else None
    if (
        spec.platform_id not in {"darwin", "linux"}
        or spec.cwd != request.view_root
        or spec.view_root != request.view_root
        or spec.scratch_root != request.scratch_root
        or spec.profile_id != "aizim-worker"
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or not spec.argv
        or not Path(spec.argv[0]).is_absolute()
        or (
            command is not None
            and (
                not command
                or not Path(command[0]).is_absolute()
                or len(spec.argv) < len(command)
                or spec.argv[-len(command) :] != command
            )
        )
    ):
        raise ValueError("invalid sandbox launch specification")


def probe_document(request: ProbeRequest) -> str:
    return json.dumps(
        {
            "view_root": str(request.view_root),
            "scratch_root": str(request.scratch_root),
            "state_database": str(request.state_database),
            "unleased_file": str(request.unleased_file),
            "shared_artifact": str(request.shared_artifact),
            "gateway_socket": str(request.gateway_socket),
            "tcp_host": request.tcp_host,
            "tcp_port": request.tcp_port,
            "allowed_view_file": str(request.allowed_view_file),
            "allowed_view_sha256": request.allowed_view_sha256,
            "secret_environment_name": request.secret_environment_name,
        },
        separators=(",", ":"),
    )


def append_probe_event(request: ProbeRequest, attempt: ProbeAttempt, policy_hash: str) -> None:
    probe_id = f"{request.probe_id}:{attempt.operation.value}"
    payload: dict[str, JsonValue]
    if attempt.verdict == "denied":
        event_type = "SandboxProbeDenied"
        payload = {
            "probe_id": probe_id,
            "reason_code": attempt.reason_code,
            "operation": attempt.operation.value,
            "policy_hash": policy_hash,
        }
    else:
        event_type = "SandboxProbePassed"
        payload = {
            "probe_id": probe_id,
            "operation": attempt.operation.value,
            "policy_hash": policy_hash,
        }
    request.event_sink.append_event(
        AppendEventCommand(
            event_type=event_type,
            actor="sandbox_adapter",
            run_id=request.run_id,
            causation_id=None,
            payload=payload,
        )
    )


def record_gate_completion(event_sink: ProbeEventSink, run_id: str, policy_hash: str) -> None:
    event_sink.append_event(
        AppendEventCommand(
            event_type="SandboxProbePassed",
            actor="security_gate",
            run_id=run_id,
            causation_id=None,
            payload={
                "probe_id": f"authority-probe:{GATE_COMPLETION_OPERATION}",
                "operation": GATE_COMPLETION_OPERATION,
                "policy_hash": policy_hash,
            },
        )
    )


def protected_asset_digests(request: ProbeRequest) -> tuple[str, str]:
    return (
        hashlib.sha256(request.unleased_file.read_bytes()).hexdigest(),
        hashlib.sha256(request.shared_artifact.read_bytes()).hexdigest(),
    )


def parse_probe_attempts(stdout: bytes) -> tuple[ProbeAttempt, ...]:
    try:
        document: JsonValue = json.loads(stdout)
        if type(document) is not dict:
            raise TypeError
        raw_attempts = document["attempts"]
        if type(raw_attempts) is not list:
            raise TypeError
        attempts = tuple(_parse_probe_attempt(raw) for raw in raw_attempts)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("invalid probe output") from error
    if tuple(attempt.operation for attempt in attempts) != tuple(ProbeOperation):
        raise ValueError("invalid probe operation sequence")
    return attempts


def _parse_probe_attempt(raw: JsonValue) -> ProbeAttempt:
    if type(raw) is not dict or len(raw) != 3:
        raise TypeError
    operation = raw.get("operation")
    verdict = raw.get("verdict")
    reason_code = raw.get("reason_code")
    if (
        type(operation) is not str
        or type(reason_code) is not str
        or reason_code not in {"SANDBOX_ENFORCED", "OPERATION_ALLOWED", "OPERATION_FAILED"}
    ):
        raise TypeError
    if verdict == "denied":
        parsed_verdict = "denied"
    elif verdict == "allowed":
        parsed_verdict = "allowed"
    else:
        raise TypeError
    return ProbeAttempt(ProbeOperation(operation), parsed_verdict, reason_code)
