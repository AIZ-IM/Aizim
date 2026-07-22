from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from aizim.domain.serialization import JsonValue, canonical_json

from . import schema_v1_orchestration as _orchestration
from . import schema_v1_validation as _validation
from .payload_validation import patch_edits_payload

_DOCUMENT_FIELDS = _validation.DOCUMENT_FIELDS
_DOCUMENT_VALIDATORS = _validation.DOCUMENT_VALIDATORS
_fields = _validation.fields
_integer_payload = _validation.integer_payload
_sha256_payload = _validation.sha256_payload
_strings_payload = _validation.strings_payload
_text_payload = _validation.text_payload
_timestamp_payload = _validation.timestamp_payload


@dataclass(slots=True)
class EventValidationError(ValueError):
    location: str
    reason: str

    def __str__(self) -> str:
        return f"{self.location}: {self.reason}"


type PayloadValidator = Callable[[JsonValue], bool]
_FIELD_VALIDATORS: Final[dict[str, Callable[[JsonValue], bool]]] = {
    "base_epoch": _sha256_payload,
    "content_hash": _sha256_payload,
    "expected_hash": _sha256_payload,
    "expected_version": _integer_payload,
    "knowledge_epoch": _integer_payload,
    "manifest": lambda value: type(value) is dict,
    "operations": lambda value: type(value) is list,
    "imports": _strings_payload,
    "dependencies": _strings_payload,
    "assumptions": _strings_payload,
    "axioms": _strings_payload,
    "evidence_links": _strings_payload,
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


_CONTRIBUTION_OPTIONAL: Final = _fields(
    "worker_id lease_id document_id payload_kind payload_hash expected_file_version "
    "expected_content_hash environment_fingerprint base_epoch knowledge_epoch candidate_name "
    "complete_type imports dependencies assumptions evidence_links edits rebased_from"
)
_CONTRIBUTION_VALIDATORS: Final = {
    "payload_kind": lambda value: type(value) is str and value in {"patch", "snapshot"},
    "payload_hash": _sha256_payload,
    "expected_file_version": _integer_payload,
    "expected_content_hash": _sha256_payload,
    "environment_fingerprint": _sha256_payload,
    "edits": patch_edits_payload,
    "base_epoch": _sha256_payload,
    "knowledge_epoch": _integer_payload,
    **dict.fromkeys(("imports", "dependencies", "assumptions", "evidence_links"), _strings_payload),
}
_DECLARATION_OPTIONAL: Final = _fields(
    "name type content_hash contribution_id module dependencies assumptions axioms evidence_links "
    "publication_sequence"
)
_DELTA_OPTIONAL: Final = _fields(
    "declaration_id contribution_id previous_base_epoch previous_knowledge_epoch "
    "fully_qualified_name complete_type module dependencies assumptions axioms evidence_links "
    "publication_sequence"
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
    "LeaseGranted": _codec(_DOCUMENT_FIELDS, validators=_DOCUMENT_VALIDATORS),
    "LeaseReleased": _codec(("lease_id",), ("reason_code",)),
    "LeaseRecovered": _codec(("lease_id",), ("reason_code",)),
    "DocumentEditPrepared": _codec(
        (*_DOCUMENT_FIELDS, "expected_version", "expected_hash"),
        validators={**_DOCUMENT_VALIDATORS, "expected_hash": _sha256_payload},
    ),
    "DocumentEdited": _codec(_DOCUMENT_FIELDS, validators=_DOCUMENT_VALIDATORS),
    "DocumentEditRecovered": _codec(_DOCUMENT_FIELDS, validators=_DOCUMENT_VALIDATORS),
    "FormalActionRecorded": _codec(
        (
            "action_id",
            "action_kind",
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
            "action_kind": lambda value: (
                type(value) is str and value in {"diagnostics", "goal", "trial"}
            ),
            "input_hash": _sha256_payload,
            "output_hash": _sha256_payload,
            "document_version": _integer_payload,
            "started_at": _timestamp_payload,
            "completed_at": _timestamp_payload,
        },
    ),
    "ContributionSubmitted": _codec(
        ("contribution_id",), _CONTRIBUTION_OPTIONAL, _CONTRIBUTION_VALIDATORS
    ),
    "ContributionRebased": _codec(
        ("contribution_id", "source_contribution_id"),
        ("base_epoch",),
        {"base_epoch": _sha256_payload},
    ),
    "ContributionEnqueued": _codec(("contribution_id",), ("state",)),
    "PromotionStateChanged": _codec(
        ("contribution_id", "state"), ("state_version", "former_owner")
    ),
    "PromotionPrepared": _codec(
        (
            "contribution_id",
            "module",
            "content_hash",
            "base_epoch",
            "knowledge_epoch",
            "declaration_id",
            "delta_id",
        ),
        validators={"content_hash": _sha256_payload, "base_epoch": _sha256_payload},
    ),
    "PromotionFailed": _codec(
        ("contribution_id", "reason_code"),
        ("artifact_hash",),
        {"artifact_hash": _sha256_payload},
    ),
    "DeclarationPublished": _codec(
        ("declaration_id",),
        _DECLARATION_OPTIONAL,
        {
            "content_hash": _sha256_payload,
            **dict.fromkeys(
                ("dependencies", "assumptions", "axioms", "evidence_links"), _strings_payload
            ),
            "publication_sequence": _integer_payload,
        },
    ),
    "KnowledgeDeltaPublished": _codec(
        ("delta_id", "base_epoch", "knowledge_epoch"),
        _DELTA_OPTIONAL,
        {
            "base_epoch": _sha256_payload,
            "previous_base_epoch": _sha256_payload,
            "previous_knowledge_epoch": _integer_payload,
            "publication_sequence": _integer_payload,
            **dict.fromkeys(
                ("dependencies", "assumptions", "axioms", "evidence_links"), _strings_payload
            ),
        },
    ),
    "KnowledgeDeltaAcknowledged": _codec(
        ("acknowledgement_id", "worker_id", "delta_id"),
        validators={"acknowledgement_id": _sha256_payload},
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
        ("transition_id", "old_fingerprint", "new_fingerprint", "reason"),
        validators={"old_fingerprint": _sha256_payload, "new_fingerprint": _sha256_payload},
    ),
    "EnvironmentTransitionApproved": _codec(
        ("transition_id", "old_fingerprint", "new_fingerprint", "reason", "reviewer"),
        validators={"old_fingerprint": _sha256_payload, "new_fingerprint": _sha256_payload},
    ),
    "EnvironmentTransitionRejected": _codec(("transition_id",), ("reason_code",)),
    "AlignmentReviewed": _codec(("review_id",), ("verdict", "reviewer", "kind")),
    "InterventionRecorded": _codec(("intervention_id",), ("kind", "actor", "reason")),
}

_orchestration.extend(_CODECS, _codec)


def validate_payload(event_type: str, payload: Mapping[str, JsonValue]) -> None:
    codec = _CODECS.get(event_type)
    if codec is None:
        raise EventValidationError("event_type", f"unknown event type {event_type!r}")
    codec.validate(event_type, payload)
