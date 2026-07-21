from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, TypedDict

from aizim.domain.serialization import JsonValue, canonical_json

EVENT_SCHEMA_VERSION: Final = 1
_EVENT_FIELDS: Final = frozenset(
    {
        "event_id",
        "schema_version",
        "event_type",
        "occurred_at",
        "actor",
        "run_id",
        "causation_id",
        "payload",
    }
)
_ULID_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


@dataclass(frozen=True, slots=True)
class EventValidationError(ValueError):
    location: str
    reason: str

    def __str__(self) -> str:
        return f"{self.location}: {self.reason}"


@dataclass(frozen=True, slots=True)
class IncompatibleEventSchemaError(RuntimeError):
    source_version: int

    def __str__(self) -> str:
        return f"INCOMPATIBLE_EVENT_SCHEMA: unsupported event schema {self.source_version}"


def _text_payload(value: JsonValue) -> bool:
    return type(value) is str and bool(value)


def _integer_payload(value: JsonValue) -> bool:
    return type(value) is int and value >= 0


_FIELD_VALIDATORS: Final[dict[str, Callable[[JsonValue], bool]]] = {
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

    def validate(self, event_type: str, payload: dict[str, JsonValue]) -> None:
        missing = self.required - payload.keys()
        unknown = payload.keys() - self.required - self.optional
        if missing:
            raise EventValidationError(event_type, f"missing fields: {', '.join(sorted(missing))}")
        if unknown:
            raise EventValidationError(event_type, f"unknown fields: {', '.join(sorted(unknown))}")
        for field, value in payload.items():
            validator = _FIELD_VALIDATORS.get(field, _text_payload)
            if not validator(value):
                raise EventValidationError(event_type, f"field {field} has an invalid type")
        canonical_json(payload)


def _codec(
    required: tuple[str, ...] = (), optional: tuple[str, ...] = ()
) -> _PayloadCodec:
    return _PayloadCodec(frozenset(required), frozenset(optional))


_CODECS: Final = {
    "ProjectInitialized": _codec(
        ("project_id", "base_epoch", "knowledge_epoch"), ("environment_fingerprint",)
    ),
    "RunCreated": _codec(optional=("manifest", "status")),
    "RunCompleted": _codec(optional=("outcome", "ended_at")),
    "RunAborted": _codec(optional=("reason_code", "ended_at")),
    "WorkerRegistered": _codec(("worker_id",), ("role", "status")),
    "WorkerStarted": _codec(("worker_id",), ("role", "started_at")),
    "WorkerStopped": _codec(("worker_id",), ("reason_code", "stopped_at")),
    "WorkerCrashed": _codec(("worker_id",), ("reason_code", "artifact_hash")),
    "CapabilityMinted": _codec(
        ("worker_id", "role"), ("token_hash", "lease_id", "operations", "expires_at")
    ),
    "CapabilityDenied": _codec(
        ("reason_code", "role", "worker_id", "operation", "request_id")
    ),
    "SandboxProbeStarted": _codec(("probe_id",), ("profile",)),
    "SandboxProbeDenied": _codec(("probe_id", "reason_code"), ("operation",)),
    "SandboxProbePassed": _codec(("probe_id",), ("operation",)),
    "SandboxProbeFailed": _codec(("probe_id", "reason_code"), ("artifact_hash",)),
    "LeaseGranted": _codec(("lease_id", "worker_id", "document_id"), ("expires_at",)),
    "LeaseReleased": _codec(("lease_id",), ("reason_code",)),
    "LeaseRecovered": _codec(("lease_id",), ("reason_code",)),
    "DocumentEditPrepared": _codec(("document_id",), ("lease_id", "expected_version")),
    "DocumentEdited": _codec(("document_id",), ("version", "content_hash", "lease_id")),
    "DocumentEditRecovered": _codec(("document_id",), ("version", "content_hash")),
    "FormalActionRecorded": _codec(
        ("action_id",),
        ("worker_id", "document_id", "input_hash", "output_hash", "verdict"),
    ),
    "ContributionSubmitted": _codec(("contribution_id",), ("worker_id", "lease_id")),
    "ContributionRebased": _codec(
        ("contribution_id", "source_contribution_id"), ("base_epoch",)
    ),
    "ContributionEnqueued": _codec(("contribution_id",), ("state",)),
    "PromotionStateChanged": _codec(("contribution_id", "state"), ("state_version",)),
    "PromotionFailed": _codec(("contribution_id", "reason_code"), ("artifact_hash",)),
    "DeclarationPublished": _codec(
        ("declaration_id",), ("name", "type", "content_hash", "contribution_id")
    ),
    "KnowledgeDeltaPublished": _codec(
        ("delta_id", "base_epoch", "knowledge_epoch"), ("declaration_id",)
    ),
    "LeanRuntimeStarted": _codec(("runtime_id",), ("mode", "started_at")),
    "LeanRuntimeCrashed": _codec(("runtime_id", "reason_code"), ("artifact_hash",)),
    "LeanRuntimeRestarted": _codec(("runtime_id",), ("previous_runtime_id",)),
    "EnvironmentTransitionProposed": _codec(("transition_id",), ("fingerprint",)),
    "EnvironmentTransitionApproved": _codec(("transition_id",), ("reviewer",)),
    "EnvironmentTransitionRejected": _codec(("transition_id",), ("reason_code",)),
    "AlignmentReviewed": _codec(("review_id",), ("verdict", "reviewer", "kind")),
    "InterventionRecorded": _codec(("intervention_id",), ("kind", "actor", "reason")),
}


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: str
    schema_version: int
    event_type: str
    occurred_at: datetime
    actor: str
    run_id: str | None
    causation_id: str | None
    payload: dict[str, JsonValue]

    def __post_init__(self) -> None:
        _text(self.event_id, "event_id")
        if len(self.event_id) != 26 or any(char not in _ULID_ALPHABET for char in self.event_id):
            raise EventValidationError("event_id", "must be a 26-character Crockford ULID")
        if self.schema_version != EVENT_SCHEMA_VERSION:
            raise IncompatibleEventSchemaError(self.schema_version)
        _text(self.event_type, "event_type")
        _text(self.actor, "actor")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() != timedelta(0):
            raise EventValidationError("occurred_at", "must be timezone-aware UTC")
        for location, value in (("run_id", self.run_id), ("causation_id", self.causation_id)):
            if value is not None:
                _text(value, location)
        codec = _CODECS.get(self.event_type)
        if codec is None:
            raise EventValidationError("event_type", f"unknown event type {self.event_type!r}")
        if type(self.payload) is not dict:
            raise EventValidationError("payload", "must be a JSON object")
        codec.validate(self.event_type, self.payload)


class EventDocument(TypedDict):
    event_id: str
    schema_version: int
    event_type: str
    occurred_at: str
    actor: str
    run_id: str | None
    causation_id: str | None
    payload: dict[str, JsonValue]


def _text(value: str, location: str) -> None:
    if type(value) is not str or not value:
        raise EventValidationError(location, "must be a non-empty string")


def _timestamp(value: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise EventValidationError("occurred_at", "must be canonical UTC RFC 3339")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise EventValidationError("occurred_at", "must be canonical UTC RFC 3339") from error
    if parsed.isoformat().replace("+00:00", "Z") != value:
        raise EventValidationError("occurred_at", "must be canonical UTC RFC 3339")
    return parsed


def _required_text_value(event_dict: Mapping[str, JsonValue], key: str) -> str:
    value = event_dict[key]
    if type(value) is not str or not value:
        raise EventValidationError(key, "must be a non-empty string")
    return value


def _optional_text_value(event_dict: Mapping[str, JsonValue], key: str) -> str | None:
    value = event_dict[key]
    if value is None:
        return None
    if type(value) is not str or not value:
        raise EventValidationError(key, "must be a non-empty string or null")
    return value


def event_as_dict(event: EventEnvelope) -> EventDocument:
    return EventDocument(
        event_id=event.event_id,
        schema_version=event.schema_version,
        event_type=event.event_type,
        occurred_at=event.occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        actor=event.actor,
        run_id=event.run_id,
        causation_id=event.causation_id,
        payload=event.payload,
    )


def _upcast_v1(event_dict: Mapping[str, JsonValue]) -> EventEnvelope:
    unknown = event_dict.keys() - _EVENT_FIELDS
    missing = _EVENT_FIELDS - event_dict.keys()
    if missing or unknown:
        details = missing if missing else unknown
        kind = "missing" if missing else "unknown"
        raise EventValidationError("event", f"{kind} fields: {', '.join(sorted(details))}")
    payload = event_dict["payload"]
    if type(payload) is not dict:
        raise EventValidationError("payload", "must be a JSON object")
    return EventEnvelope(
        event_id=_required_text_value(event_dict, "event_id"),
        schema_version=EVENT_SCHEMA_VERSION,
        event_type=_required_text_value(event_dict, "event_type"),
        occurred_at=_timestamp(_required_text_value(event_dict, "occurred_at")),
        actor=_required_text_value(event_dict, "actor"),
        run_id=_optional_text_value(event_dict, "run_id"),
        causation_id=_optional_text_value(event_dict, "causation_id"),
        payload=payload,
    )


_UPCASTERS: Final[dict[int, Callable[[Mapping[str, JsonValue]], EventEnvelope]]] = {
    1: _upcast_v1
}


def upcast(event_dict: Mapping[str, JsonValue]) -> EventEnvelope:
    source_version = event_dict.get("schema_version")
    if type(source_version) is not int:
        raise EventValidationError("schema_version", "must be an integer")
    upcaster = _UPCASTERS.get(source_version)
    if upcaster is None:
        raise IncompatibleEventSchemaError(source_version)
    return upcaster(event_dict)


class MonotoneUlidFactory:
    def __init__(self) -> None:
        self._last_milliseconds = -1
        self._randomness = 0

    def __call__(self) -> str:
        milliseconds = time.time_ns() // 1_000_000
        if milliseconds > self._last_milliseconds:
            self._last_milliseconds = milliseconds
            self._randomness = secrets.randbits(80)
        else:
            self._randomness += 1
            if self._randomness >= 1 << 80:
                self._last_milliseconds += 1
                self._randomness = 0
        value = (self._last_milliseconds << 80) | self._randomness
        characters = ["0"] * 26
        for index in range(25, -1, -1):
            characters[index] = _ULID_ALPHABET[value & 31]
            value >>= 5
        return "".join(characters)


def utc_now() -> datetime:
    return datetime.now(UTC)
