from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Final

import pytest

from aizim.domain.serialization import JsonValue
from aizim.state import (
    AppendEventCommand,
    EventValidationError,
    StateService,
    StateServiceConfig,
)
from aizim.state.capabilities import CapabilityRecord
from aizim.state.events import EventEnvelope, event_as_dict
from aizim.state.store_contracts import ProjectionAuthorityError

_HASH: Final = "a" * 64
_TIMESTAMP: Final = "2026-07-21T10:00:00Z"
_HASH_CASES: Final = (
    ("ProjectInitialized", "base_epoch", {"project_id": "p", "knowledge_epoch": 0}),
    (
        "ProjectInitialized",
        "environment_fingerprint",
        {"project_id": "p", "base_epoch": _HASH, "knowledge_epoch": 0},
    ),
    ("WorkerCrashed", "artifact_hash", {"worker_id": "w"}),
    ("SandboxProbeFailed", "artifact_hash", {"probe_id": "p", "reason_code": "failed"}),
    ("DocumentEdited", "content_hash", {"document_id": "d"}),
    ("DocumentEditRecovered", "content_hash", {"document_id": "d"}),
    ("FormalActionRecorded", "input_hash", {"action_id": "a"}),
    ("FormalActionRecorded", "output_hash", {"action_id": "a"}),
    ("ContributionRebased", "base_epoch", {"contribution_id": "c", "source_contribution_id": "s"}),
    ("PromotionFailed", "artifact_hash", {"contribution_id": "c", "reason_code": "failed"}),
    ("DeclarationPublished", "content_hash", {"declaration_id": "d"}),
    (
        "KnowledgeDeltaPublished",
        "base_epoch",
        {"delta_id": "d", "knowledge_epoch": 1},
    ),
    ("LeanRuntimeCrashed", "artifact_hash", {"runtime_id": "r", "reason_code": "failed"}),
    ("EnvironmentTransitionProposed", "fingerprint", {"transition_id": "t"}),
)
_TIMESTAMP_CASES: Final = (
    ("RunCompleted", "ended_at", {}),
    ("RunAborted", "ended_at", {}),
    ("WorkerStarted", "started_at", {"worker_id": "w"}),
    ("WorkerStopped", "stopped_at", {"worker_id": "w"}),
    (
        "CapabilityMinted",
        "expires_at",
        {"worker_id": "w", "role": "formalizer", "operations": ["project.read"]},
    ),
    ("LeaseGranted", "expires_at", {"lease_id": "l", "worker_id": "w", "document_id": "d"}),
    ("LeanRuntimeStarted", "started_at", {"runtime_id": "r"}),
)


def _event(event_type: str, payload: dict[str, JsonValue]) -> EventEnvelope:
    return EventEnvelope(
        event_id="01J00000000000000000000000",
        schema_version=1,
        event_type=event_type,
        occurred_at=datetime(2026, 7, 21, 10, tzinfo=UTC),
        actor="supervisor",
        run_id=None,
        causation_id=None,
        payload=payload,
    )


def _with_field(
    payload: dict[str, JsonValue], field: str, value: JsonValue
) -> dict[str, JsonValue]:
    changed = dict(payload)
    changed[field] = value
    return changed


@pytest.mark.parametrize(("event_type", "field", "payload"), _HASH_CASES)
def test_schema_v1_rejects_non_lowercase_sha256_for_every_hash_binding(
    event_type: str, field: str, payload: dict[str, JsonValue]
) -> None:
    # Given / When / Then
    with pytest.raises(EventValidationError):
        _event(event_type, _with_field(payload, field, "A" * 64))


@pytest.mark.parametrize(("event_type", "field", "payload"), _HASH_CASES)
def test_schema_v1_accepts_lowercase_sha256_for_every_hash_binding(
    event_type: str, field: str, payload: dict[str, JsonValue]
) -> None:
    # Given / When
    event = _event(event_type, _with_field(payload, field, _HASH))

    # Then
    assert event.event_type == event_type


@pytest.mark.parametrize(("event_type", "field", "payload"), _TIMESTAMP_CASES)
def test_schema_v1_rejects_noncanonical_timestamp_for_every_time_binding(
    event_type: str, field: str, payload: dict[str, JsonValue]
) -> None:
    # Given / When / Then
    with pytest.raises(EventValidationError):
        _event(event_type, _with_field(payload, field, "2026-07-21T10:00:00+00:00"))


@pytest.mark.parametrize(("event_type", "field", "payload"), _TIMESTAMP_CASES)
def test_schema_v1_accepts_canonical_timestamp_for_every_time_binding(
    event_type: str, field: str, payload: dict[str, JsonValue]
) -> None:
    # Given / When
    event = _event(event_type, _with_field(payload, field, _TIMESTAMP))

    # Then
    assert event.event_type == event_type


def test_event_payload_is_recursively_detached_and_immutable() -> None:
    # Given
    labels: list[JsonValue] = ["original"]
    manifest: dict[str, JsonValue] = {"labels": labels}
    payload: dict[str, JsonValue] = {"manifest": manifest}

    # When
    event = _event("RunCreated", payload)
    labels.append("mutated")
    manifest["owner"] = "mutated"
    payload["status"] = "mutated"

    # Then
    assert isinstance(event.payload, MappingProxyType)
    frozen_manifest = event.payload["manifest"]
    assert isinstance(frozen_manifest, MappingProxyType)
    assert frozen_manifest["labels"] == ("original",)
    assert event_as_dict(event)["payload"] == {"manifest": {"labels": ["original"]}}


def test_schema_v1_accepts_exact_parent_capability_minted_shape() -> None:
    payload: dict[str, JsonValue] = {
        "worker_id": "worker-1",
        "role": "formalizer",
        "token_hash": _HASH,
    }

    event = _event("CapabilityMinted", payload)

    assert event_as_dict(event)["payload"] == payload


@pytest.mark.parametrize("operations", [None, [], ["project.read", "project.read"], [None]])
def test_exact_parent_capability_minted_event_survives_restart(
    tmp_path: Path, operations: list[JsonValue] | None
) -> None:
    payload: dict[str, JsonValue] = {
        "worker_id": "worker-1",
        "role": "formalizer",
        "token_hash": _HASH,
    }
    if operations is not None:
        payload["operations"] = operations
    command = AppendEventCommand("CapabilityMinted", "supervisor", "run-1", None, payload)
    with StateService(StateServiceConfig(tmp_path, "parent-writer")) as service:
        service.append_event(command)

    with StateService(StateServiceConfig(tmp_path, "current-reader")) as restarted:
        events = restarted.query_events("run-1")
        replay = restarted.replay_verify()

    assert len(events) == 1
    assert event_as_dict(events[0].envelope)["payload"] == payload
    assert replay.matched


@pytest.mark.parametrize("event_type", ["LeaseReleased", "LeaseRecovered"])
def test_runless_lease_terminal_event_rolls_back_and_keeps_capability_live(
    tmp_path: Path, event_type: str
) -> None:
    capability = CapabilityRecord(
        token_hash=_HASH,
        run_id="run-1",
        worker_id="worker-1",
        role="formalizer",
        lease_id="lease-1",
        operations=("project.read",),
        expires_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    command = AppendEventCommand(event_type, "broker", None, None, {"lease_id": "lease-1"})
    with StateService(StateServiceConfig(tmp_path, "service-session")) as service:
        service.persist_capability(capability)
        before = service.query_events()
        with pytest.raises(ProjectionAuthorityError):
            service.append_event(command)
        assert service.query_events() == before
        assert service.query_projection("leases", "lease-1") is None
        assert service.capability_record(_HASH) == capability


def test_serialized_event_payload_is_a_detached_mutable_document() -> None:
    # Given
    event = _event("RunCreated", {"manifest": {"labels": ["original"]}})

    # When
    document = event_as_dict(event)
    document["payload"]["status"] = "changed"
    manifest = document["payload"]["manifest"]
    assert type(manifest) is dict
    labels = manifest["labels"]
    assert type(labels) is list
    labels.append("changed")

    # Then
    assert event_as_dict(event)["payload"] == {"manifest": {"labels": ["original"]}}


def test_append_command_detaches_payload_before_later_service_use(tmp_path: Path) -> None:
    # Given
    labels: list[JsonValue] = ["original"]
    payload: dict[str, JsonValue] = {"manifest": {"labels": labels}}
    command = AppendEventCommand("RunCreated", "supervisor", "run-1", None, payload)
    labels.append("mutated")
    payload["status"] = "mutated"

    # When
    with StateService(StateServiceConfig(tmp_path, "service-session")) as service:
        record = service.append_event(command)

    # Then
    assert event_as_dict(record.envelope)["payload"] == {
        "manifest": {"labels": ["original"]}
    }
