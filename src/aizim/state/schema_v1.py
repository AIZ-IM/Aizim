from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final

from aizim.domain.serialization import JsonValue, canonical_json


@dataclass(frozen=True, slots=True)
class EventValidationError(ValueError):
    location: str
    reason: str

    def __str__(self) -> str:
        return f"{self.location}: {self.reason}"


def _text_payload(value: JsonValue) -> bool:
    return type(value) is str and bool(value)


def _integer_payload(value: JsonValue) -> bool:
    return type(value) is int and value >= 0


def _sha256_payload(value: JsonValue) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _timestamp_payload(value: JsonValue) -> bool:
    if type(value) is not str or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return parsed.isoformat().replace("+00:00", "Z") == value


type PayloadValidator = Callable[[JsonValue], bool]
_FIELD_VALIDATORS: Final[dict[str, Callable[[JsonValue], bool]]] = {
    "base_epoch": _sha256_payload,
    "content_hash": _sha256_payload,
    "expected_hash": _sha256_payload,
    "expected_version": _integer_payload,
    "knowledge_epoch": _integer_payload,
    "manifest": lambda value: type(value) is dict,
    "operations": lambda value: type(value) is list,
    "state_version": _integer_payload,
    "version": _integer_payload,
}


@dataclass(frozen=True, slots=True)
class _PayloadCodec:
    required: frozenset[str]
    optional: frozenset[str]
    validators: Mapping[str, PayloadValidator]

    def validate(self, event_type: str, payload: Mapping[str, JsonValue]) -> None:
        missing = self.required - payload.keys()
        unknown = payload.keys() - self.required - self.optional
        if missing:
            raise EventValidationError(event_type, f"missing fields: {', '.join(sorted(missing))}")
        if unknown:
            raise EventValidationError(event_type, f"unknown fields: {', '.join(sorted(unknown))}")
        for field, value in payload.items():
            validator = self.validators.get(field, _FIELD_VALIDATORS.get(field, _text_payload))
            if not validator(value):
                raise EventValidationError(event_type, f"field {field} has an invalid type")
        canonical_json(payload)


def _codec(
    required: tuple[str, ...] = (),
    optional: tuple[str, ...] = (),
    validators: Mapping[str, PayloadValidator] | None = None,
) -> _PayloadCodec:
    return _PayloadCodec(
        frozenset(required),
        frozenset(optional),
        MappingProxyType({} if validators is None else dict(validators)),
    )


_CODECS: Final = {
    "ProjectInitialized": _codec(
        ("project_id", "base_epoch", "knowledge_epoch"),
        ("environment_fingerprint",),
        {"base_epoch": _sha256_payload, "environment_fingerprint": _sha256_payload},
    ),
    "RunCreated": _codec(optional=("manifest", "status")),
    "RunCompleted": _codec(
        optional=("outcome", "ended_at"), validators={"ended_at": _timestamp_payload}
    ),
    "RunAborted": _codec(
        optional=("reason_code", "ended_at"), validators={"ended_at": _timestamp_payload}
    ),
    "WorkerRegistered": _codec(("worker_id",), ("role", "status")),
    "WorkerStarted": _codec(
        ("worker_id",), ("role", "started_at"), {"started_at": _timestamp_payload}
    ),
    "WorkerStopped": _codec(
        ("worker_id",), ("reason_code", "stopped_at"), {"stopped_at": _timestamp_payload}
    ),
    "WorkerCrashed": _codec(
        ("worker_id",), ("reason_code", "artifact_hash"), {"artifact_hash": _sha256_payload}
    ),
    "CapabilityMinted": _codec(
        ("worker_id", "role"),
        ("token_hash", "lease_id", "operations", "expires_at"),
        {"token_hash": _sha256_payload, "expires_at": _timestamp_payload},
    ),
    "CapabilityDenied": _codec(("reason_code", "role", "worker_id", "operation", "request_id")),
    "SandboxProbeStarted": _codec(("probe_id",), ("profile",)),
    "SandboxProbeDenied": _codec(("probe_id", "reason_code"), ("operation",)),
    "SandboxProbePassed": _codec(("probe_id",), ("operation",)),
    "SandboxProbeFailed": _codec(
        ("probe_id", "reason_code"), ("artifact_hash",), {"artifact_hash": _sha256_payload}
    ),
    "LeaseGranted": _codec(
        (
            "lease_id",
            "worker_id",
            "document_id",
            "relative_path",
            "virtual_document_namespace",
            "base_epoch",
            "knowledge_epoch",
            "version",
            "content_hash",
            "expires_at",
        ),
        validators={
            "base_epoch": _sha256_payload,
            "content_hash": _sha256_payload,
            "expires_at": _timestamp_payload,
        },
    ),
    "LeaseReleased": _codec(("lease_id",), ("reason_code",)),
    "LeaseRecovered": _codec(("lease_id",), ("reason_code",)),
    "DocumentEditPrepared": _codec(
        (
            "document_id",
            "lease_id",
            "worker_id",
            "relative_path",
            "virtual_document_namespace",
            "base_epoch",
            "knowledge_epoch",
            "version",
            "content_hash",
            "expires_at",
            "expected_version",
            "expected_hash",
        ),
        validators={
            "base_epoch": _sha256_payload,
            "content_hash": _sha256_payload,
            "expected_hash": _sha256_payload,
            "expires_at": _timestamp_payload,
        },
    ),
    "DocumentEdited": _codec(
        (
            "document_id",
            "lease_id",
            "worker_id",
            "relative_path",
            "virtual_document_namespace",
            "base_epoch",
            "knowledge_epoch",
            "version",
            "content_hash",
            "expires_at",
        ),
        validators={
            "base_epoch": _sha256_payload,
            "content_hash": _sha256_payload,
            "expires_at": _timestamp_payload,
        },
    ),
    "DocumentEditRecovered": _codec(
        (
            "document_id",
            "lease_id",
            "worker_id",
            "relative_path",
            "virtual_document_namespace",
            "base_epoch",
            "knowledge_epoch",
            "version",
            "content_hash",
            "expires_at",
        ),
        validators={
            "base_epoch": _sha256_payload,
            "content_hash": _sha256_payload,
            "expires_at": _timestamp_payload,
        },
    ),
    "FormalActionRecorded": _codec(
        (
            "action_id",
            "worker_id",
            "document_id",
            "input_hash",
            "output_hash",
            "verdict",
            "document_version",
            "base_epoch",
            "knowledge_epoch",
            "started_at",
            "completed_at",
        ),
        validators={
            "input_hash": _sha256_payload,
            "output_hash": _sha256_payload,
            "document_version": _integer_payload,
            "started_at": _timestamp_payload,
            "completed_at": _timestamp_payload,
        },
    ),
    "ContributionSubmitted": _codec(("contribution_id",), ("worker_id", "lease_id")),
    "ContributionRebased": _codec(
        ("contribution_id", "source_contribution_id"),
        ("base_epoch",),
        {"base_epoch": _sha256_payload},
    ),
    "ContributionEnqueued": _codec(("contribution_id",), ("state",)),
    "PromotionStateChanged": _codec(("contribution_id", "state"), ("state_version",)),
    "PromotionFailed": _codec(
        ("contribution_id", "reason_code"),
        ("artifact_hash",),
        {"artifact_hash": _sha256_payload},
    ),
    "DeclarationPublished": _codec(
        ("declaration_id",),
        ("name", "type", "content_hash", "contribution_id"),
        {"content_hash": _sha256_payload},
    ),
    "KnowledgeDeltaPublished": _codec(
        ("delta_id", "base_epoch", "knowledge_epoch"),
        ("declaration_id",),
        {"base_epoch": _sha256_payload},
    ),
    "LeanRuntimeStarted": _codec(
        ("runtime_id",), ("mode", "started_at"), {"started_at": _timestamp_payload}
    ),
    "LeanRuntimeCrashed": _codec(
        ("runtime_id", "reason_code"),
        ("artifact_hash",),
        {"artifact_hash": _sha256_payload},
    ),
    "LeanRuntimeRestarted": _codec(("runtime_id",), ("previous_runtime_id",)),
    "EnvironmentTransitionProposed": _codec(
        ("transition_id",), ("fingerprint",), {"fingerprint": _sha256_payload}
    ),
    "EnvironmentTransitionApproved": _codec(("transition_id",), ("reviewer",)),
    "EnvironmentTransitionRejected": _codec(("transition_id",), ("reason_code",)),
    "AlignmentReviewed": _codec(("review_id",), ("verdict", "reviewer", "kind")),
    "InterventionRecorded": _codec(("intervention_id",), ("kind", "actor", "reason")),
}


def validate_payload(event_type: str, payload: Mapping[str, JsonValue]) -> None:
    codec = _CODECS.get(event_type)
    if codec is None:
        raise EventValidationError("event_type", f"unknown event type {event_type!r}")
    codec.validate(event_type, payload)
