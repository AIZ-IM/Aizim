from __future__ import annotations

import asyncio
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Never, assert_never

from aizim import foundation_contract as contract
from aizim.domain.serialization import JsonValue
from aizim.foundation_artifacts import FoundationArtifactError, read_registered_artifact
from aizim.runtime.state_process import validate_live_state_process
from aizim.state import StateService, StateServiceConfig
from aizim.state.operations import RpcFailure, RpcRequest, RpcResponse, RpcSuccess
from aizim.state.rpc import rpc_call

_HASH = re.compile(r"[0-9a-f]{64}")
type JsonObject = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class FoundationError(RuntimeError):
    reason: str


@dataclass(frozen=True, slots=True)
class EventEvidence:
    sequence: int
    event_id: str
    event_type: str
    run_id: str | None
    payload: JsonObject


@dataclass(frozen=True, slots=True)
class ArtifactEvidence:
    name: str
    digest: str
    body: bytes


@dataclass(frozen=True, slots=True)
class RunSelection:
    run_id: str
    manifest: JsonObject
    created_sequence: int


@dataclass(frozen=True, slots=True)
class FoundationEvidence:
    events: tuple[EventEvidence, ...]
    artifacts: tuple[ArtifactEvidence, ...]
    selection: RunSelection
    replay_digest: str


@dataclass(frozen=True, slots=True)
class _Snapshot:
    events: tuple[EventEvidence, ...]
    projections: tuple[JsonObject, ...]
    replay_digest: str


def fail(reason: str) -> Never:
    raise FoundationError(reason)


def is_hash(value: JsonValue) -> bool:
    return type(value) is str and _HASH.fullmatch(value) is not None


def _object(value: JsonValue, reason: str) -> JsonObject:
    if type(value) is not dict:
        fail(reason)
    return value


def _items(value: JsonValue, reason: str) -> list[JsonValue]:
    if type(value) is not list:
        fail(reason)
    return value


def _text(value: JsonValue, reason: str) -> str:
    if type(value) is not str or not value:
        fail(reason)
    return value


def _integer(value: JsonValue, reason: str) -> int:
    if type(value) is not int or value < 0:
        fail(reason)
    return value


def _rpc_result(response: RpcResponse, reason: str) -> JsonValue:
    match response:
        case RpcSuccess(result=result):
            return result
        case RpcFailure():
            fail(reason)
        case unreachable:
            assert_never(unreachable)


def _events(value: JsonValue) -> tuple[EventEvidence, ...]:
    parsed: list[EventEvidence] = []
    for raw in _items(value, "EVENT_RESPONSE_INVALID"):
        document = _object(raw, "EVENT_RESPONSE_INVALID")
        sequence = _integer(document.get("sequence"), "EVENT_RESPONSE_INVALID")
        run_id = document.get("run_id")
        if run_id is not None and type(run_id) is not str:
            fail("EVENT_RESPONSE_INVALID")
        parsed.append(
            EventEvidence(
                sequence,
                _text(document.get("event_id"), "EVENT_RESPONSE_INVALID"),
                _text(document.get("event_type"), "EVENT_RESPONSE_INVALID"),
                run_id,
                _object(document.get("payload"), "EVENT_RESPONSE_INVALID"),
            )
        )
    return tuple(parsed)


def _projections(value: JsonValue) -> tuple[JsonObject, ...]:
    return tuple(
        _object(item, "ARTIFACT_RESPONSE_INVALID")
        for item in _items(value, "ARTIFACT_RESPONSE_INVALID")
    )


async def _read_socket(socket_path: Path) -> _Snapshot:
    event_response, artifact_response, replay_response = await asyncio.gather(
        rpc_call(socket_path, RpcRequest("query_events", {}, None)),
        rpc_call(
            socket_path,
            RpcRequest("query_projections", {"projection_name": "artifacts"}, None),
        ),
        rpc_call(socket_path, RpcRequest("replay_verify", {}, None)),
    )
    replay = _object(_rpc_result(replay_response, "REPLAY_QUERY_FAILED"), "REPLAY_RESPONSE_INVALID")
    digest = replay.get("logical_digest")
    if replay.get("matched") is not True or not is_hash(digest):
        fail("REPLAY_FAILED")
    return _Snapshot(
        _events(_rpc_result(event_response, "EVENT_QUERY_FAILED")),
        _projections(_rpc_result(artifact_response, "ARTIFACT_QUERY_FAILED")),
        _text(digest, "REPLAY_RESPONSE_INVALID"),
    )


async def _snapshot(project: Path) -> _Snapshot:
    socket_path = project / ".aizim" / "run" / "state.sock"
    if socket_path.exists():
        validate_live_state_process(project / ".aizim" / "run" / "state.pid", socket_path)
        return await _read_socket(socket_path)
    async with StateService(StateServiceConfig(project, secrets.token_urlsafe(32))) as state:
        return await _read_socket(state.socket_path)


def _select(events: tuple[EventEvidence, ...], requested: str) -> RunSelection:
    candidates: list[RunSelection] = []
    for event in events:
        if event.event_type != "RunCreated" or event.run_id is None:
            continue
        manifest = event.payload.get("manifest")
        if type(manifest) is not dict:
            continue
        if manifest.get("agent_harness_name") == manifest.get("model_backend") == "codex":
            candidates.append(RunSelection(event.run_id, manifest, event.sequence))
    if requested == "latest-real":
        if not candidates:
            fail("REAL_RUN_MISSING")
        return max(candidates, key=lambda item: item.created_sequence)
    matches = [candidate for candidate in candidates if candidate.run_id == requested]
    if len(matches) != 1:
        fail("REAL_RUN_MISSING")
    return matches[0]


def _artifacts(
    project: Path, selection: RunSelection, projections: tuple[JsonObject, ...]
) -> tuple[ArtifactEvidence, ...]:
    found: list[ArtifactEvidence] = []
    for document in projections:
        state = _object(document.get("state"), "ARTIFACT_RESPONSE_INVALID")
        if state.get("run_id") != selection.run_id:
            continue
        payload = _object(state.get("payload"), "ARTIFACT_RESPONSE_INVALID")
        name = _text(payload.get("artifact_name"), "ARTIFACT_REGISTRATION_INVALID")
        digest = _text(payload.get("content_hash"), "ARTIFACT_REGISTRATION_INVALID")
        relative = _text(payload.get("relative_path"), "ARTIFACT_REGISTRATION_INVALID")
        length = _integer(payload.get("byte_length"), "ARTIFACT_REGISTRATION_INVALID")
        if not is_hash(digest):
            fail("ARTIFACT_REGISTRATION_INVALID")
        try:
            body = read_registered_artifact(
                project, selection.run_id, name, relative, length, digest
            )
        except FoundationArtifactError as error:
            fail(str(error))
        found.append(ArtifactEvidence(name, digest, body))
    if len(found) != 5 or {artifact.name for artifact in found} != contract.ARTIFACT_NAMES:
        fail("ARTIFACT_SET_INCOMPLETE")
    return tuple(found)


async def load_evidence(project: Path, requested: str) -> FoundationEvidence:
    canonical_project = project.resolve(strict=True)
    snapshot = await _snapshot(canonical_project)
    selection = _select(snapshot.events, requested)
    return FoundationEvidence(
        snapshot.events,
        _artifacts(canonical_project, selection, snapshot.projections),
        selection,
        snapshot.replay_digest,
    )


def artifact(evidence: FoundationEvidence, name: str) -> ArtifactEvidence:
    return next(item for item in evidence.artifacts if item.name == name)


def json_artifact(evidence: FoundationEvidence, name: str) -> JsonObject:
    try:
        value: JsonValue = json.loads(artifact(evidence, name).body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FoundationError("ARTIFACT_JSON_INVALID") from error
    return _object(value, "ARTIFACT_JSON_INVALID")


def gate_policy(events: tuple[EventEvidence, ...], before_sequence: int) -> str:
    groups: dict[str, list[EventEvidence]] = {}
    for event in events:
        if (
            event.sequence < before_sequence
            and event.run_id is not None
            and event.event_type
            in {"SandboxProbeDenied", "SandboxProbeFailed", "SandboxProbePassed"}
        ):
            groups.setdefault(event.run_id, []).append(event)
    if not groups:
        fail("GATE_B_EVIDENCE_MISSING")
    records = max(groups.values(), key=lambda group: max(item.sequence for item in group))
    attempts = tuple(
        item
        for item in records
        if item.payload.get("operation") in contract.DENIED_OPERATIONS | contract.ALLOWED_OPERATIONS
    )
    terminals = tuple(
        item
        for item in records
        if item.event_type == "SandboxProbePassed"
        and item.payload.get("operation") == contract.GATE_COMPLETION_OPERATION
    )
    if (
        len(attempts) != 11
        or len(terminals) != 1
        or len(records) != 12
        or terminals[0].sequence != max(item.sequence for item in records)
    ):
        fail("GATE_B_EVIDENCE_MISSING")
    denied = {
        operation
        for item in attempts
        if item.event_type == "SandboxProbeDenied"
        and type(operation := item.payload.get("operation")) is str
    }
    allowed = {
        operation
        for item in attempts
        if item.event_type == "SandboxProbePassed"
        and type(operation := item.payload.get("operation")) is str
    }
    hashes = {value for item in records if type(value := item.payload.get("policy_hash")) is str}
    if (
        denied != contract.DENIED_OPERATIONS
        or allowed != contract.ALLOWED_OPERATIONS
        or len(hashes) != 1
    ):
        fail("GATE_B_EVIDENCE_MISSING")
    policy_hash = next(iter(hashes))
    complete_hashes = all(item.payload.get("policy_hash") == policy_hash for item in records)
    enforced = all(
        item.event_type != "SandboxProbeDenied"
        or item.payload.get("reason_code") == "SANDBOX_ENFORCED"
        for item in records
    )
    if not is_hash(policy_hash) or not complete_hashes or not enforced:
        fail("GATE_B_EVIDENCE_MISSING")
    return policy_hash
