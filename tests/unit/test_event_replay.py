from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aizim.domain.serialization import JsonValue
from aizim.state import (
    PROJECTION_NAMES,
    AppendEventCommand,
    EventValidationError,
    IncompatibleEventSchemaError,
    StateDependencies,
    StateService,
    StateServiceConfig,
    upcast,
)
from aizim.state.events import EventEnvelope


class MonotoneIds:
    def __init__(self) -> None:
        self._next = 0

    def __call__(self) -> str:
        value = f"{self._next:023d}"
        self._next += 1
        return f"01J{value}"


def _service(project_root: Path) -> StateService:
    return StateService(
        StateServiceConfig(project_root=project_root, service_session="service-session"),
        StateDependencies(
            clock=lambda: datetime(2026, 7, 21, 10, tzinfo=UTC),
            event_ids=MonotoneIds(),
        ),
    )


def _append_replay_fixture(service: StateService) -> None:
    service.append_event(
        AppendEventCommand(
            event_type="ProjectInitialized",
            actor="supervisor",
            run_id=None,
            causation_id=None,
            payload={
                "project_id": "project-1",
                "base_epoch": "a" * 64,
                "knowledge_epoch": 0,
            },
        )
    )
    service.append_event(
        AppendEventCommand(
            event_type="RunCreated",
            actor="supervisor",
            run_id="run-1",
            causation_id=None,
            payload={},
        )
    )
    service.append_event(
        AppendEventCommand(
            event_type="KnowledgeDeltaPublished",
            actor="promotion-service",
            run_id="run-1",
            causation_id=None,
            payload={
                "delta_id": "delta-1",
                "base_epoch": "b" * 64,
                "knowledge_epoch": 1,
            },
        )
    )


def test_replay_produces_identical_projection_json_and_digest(tmp_path: Path) -> None:
    # Given
    with _service(tmp_path) as service:
        _append_replay_fixture(service)
        expected_json = service.canonical_projection_json()
        expected_digest = service.logical_digest()

        # When
        verification = service.replay_verify()

    # Then
    assert verification.matched
    assert verification.projection_json == expected_json
    assert verification.logical_digest == expected_digest


def test_logical_digest_excludes_denial_audit_projection(tmp_path: Path) -> None:
    # Given
    with _service(tmp_path) as service:
        _append_replay_fixture(service)
        original_digest = service.logical_digest()

        # When
        service.append_event(
            AppendEventCommand(
                event_type="CapabilityDenied",
                actor="gateway",
                run_id="run-1",
                causation_id=None,
                payload={
                    "reason_code": "UNKNOWN_TOKEN",
                    "role": "proof_worker",
                    "worker_id": "worker-1",
                    "operation": "query_state",
                    "request_id": "request-1",
                },
            )
        )

        # Then
        assert service.logical_digest() == original_digest
        assert service.query_projection("denials", "request-1") is not None


def test_event_envelope_has_exact_immutable_contract_fields() -> None:
    assert tuple(field.name for field in fields(EventEnvelope)) == (
        "event_id",
        "schema_version",
        "event_type",
        "occurred_at",
        "actor",
        "run_id",
        "causation_id",
        "payload",
    )
    dataclass_params = getattr(EventEnvelope, "__dataclass_params__", None)
    assert dataclass_params is not None and dataclass_params.frozen


@pytest.mark.parametrize(
    "raw",
    [
        {
            "event_id": "01J00000000000000000000000",
            "schema_version": 1,
            "event_type": "UnknownEvent",
            "occurred_at": "2026-07-21T10:00:00Z",
            "actor": "supervisor",
            "run_id": None,
            "causation_id": None,
            "payload": {},
        },
        {
            "event_id": "01J00000000000000000000000",
            "schema_version": 1,
            "event_type": "ProjectInitialized",
            "occurred_at": "2026-07-21T10:00:00Z",
            "actor": "supervisor",
            "run_id": None,
            "causation_id": None,
            "payload": {"unknown_field": True},
        },
        {
            "event_id": "01J00000000000000000000000",
            "schema_version": 1,
            "event_type": "ProjectInitialized",
            "occurred_at": "2026-07-21T10:00:00Z",
            "actor": "supervisor",
            "run_id": None,
            "causation_id": None,
            "payload": {},
            "extra_envelope_field": True,
        },
    ],
)
def test_schema_v1_upcast_rejects_unknown_types_and_fields(raw: dict[str, JsonValue]) -> None:
    with pytest.raises(EventValidationError):
        upcast(raw)


def test_upcast_has_no_future_schema_catch_all() -> None:
    raw = {
        "event_id": "01J00000000000000000000000",
        "schema_version": 2,
        "event_type": "ProjectInitialized",
        "occurred_at": "2026-07-21T10:00:00Z",
        "actor": "supervisor",
        "run_id": None,
        "causation_id": None,
        "payload": {},
    }

    with pytest.raises(IncompatibleEventSchemaError, match="INCOMPATIBLE_EVENT_SCHEMA"):
        upcast(raw)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("project_id", 1),
        ("base_epoch", False),
        ("knowledge_epoch", "zero"),
    ],
)
def test_schema_v1_codec_rejects_wrong_payload_field_type(
    field_name: str, invalid_value: JsonValue
) -> None:
    payload: dict[str, JsonValue] = {
        "project_id": "project-1",
        "base_epoch": "a" * 64,
        "knowledge_epoch": 0,
    }
    payload[field_name] = invalid_value
    raw: dict[str, JsonValue] = {
        "event_id": "01J00000000000000000000000",
        "schema_version": 1,
        "event_type": "ProjectInitialized",
        "occurred_at": "2026-07-21T10:00:00Z",
        "actor": "supervisor",
        "run_id": None,
        "causation_id": None,
        "payload": payload,
    }

    with pytest.raises(EventValidationError):
        upcast(raw)


def test_projection_registry_is_fixed() -> None:
    assert (
        "research",
        "project",
        "runs",
        "artifacts",
        "evaluations",
        "schedules",
        "controller",
        "workers",
        "worker_roster",
        *("worker_assignments", "controller_runtime", "worker_executions"),
        "worker_cursors",
        "epochs",
        "leases",
        "documents",
        "formal_actions",
        "contributions",
        "verified_declarations",
        "knowledge_deltas",
        "knowledge_acknowledgements",
        "denials",
        "resources",
        "alignment_reviews",
        "interventions",
    ) == PROJECTION_NAMES


def test_identical_artifact_content_can_be_registered_by_distinct_runs(tmp_path: Path) -> None:
    with _service(tmp_path) as service:
        for run_id in ("run-1", "run-2"):
            service.append_event(
                AppendEventCommand(
                    "ArtifactRegistered",
                    "evaluation_artifacts",
                    run_id,
                    None,
                    {
                        "artifact_name": "alignment-review.json",
                        "content_hash": "d" * 64,
                        "relative_path": f".aizim/artifacts/{run_id}/alignment-review.json",
                        "media_type": "application/json",
                        "byte_length": 20,
                    },
                )
            )

        assert len(service.projections("artifacts")) == 2
        assert service.replay_verify().matched


def test_active_document_leases_ignore_minimal_terminal_lease_projection(tmp_path: Path) -> None:
    with _service(tmp_path) as service:
        service.append_event(
            AppendEventCommand(
                event_type="LeaseReleased",
                actor="capability-service",
                run_id="run-1",
                causation_id=None,
                payload={"lease_id": "non-document-lease", "reason_code": "RELEASED"},
            )
        )

        assert service.active_document_leases() == ()
