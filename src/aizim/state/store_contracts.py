from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from aizim.domain.serialization import JsonValue, canonical_json, sha256_bytes

from .events import EventEnvelope, EventValidationError, upcast
from .projections import AUDIT_PROJECTIONS, ProjectionRecord

type InitializationCheckpoint = Callable[[], None]


def continue_initialization() -> None:
    return


@dataclass(frozen=True, slots=True)
class DuplicateEventError(RuntimeError):
    event_id: str

    def __str__(self) -> str:
        return f"duplicate event id: {self.event_id}"


@dataclass(frozen=True, slots=True)
class ProjectionAuthorityError(RuntimeError):
    projection_name: str
    reason: str

    def __str__(self) -> str:
        return f"projection {self.projection_name}: {self.reason}"


@dataclass(frozen=True, slots=True)
class StoreHealth:
    journal_mode: str
    foreign_keys: bool
    synchronous: str
    busy_timeout_ms: int
    event_schema_version: int


@dataclass(frozen=True, slots=True)
class EventRecord:
    sequence: int
    envelope: EventEnvelope


def _event_record(
    row: tuple[int, str, int, str, str, str, str | None, str | None, str],
) -> EventRecord:
    sequence, event_id, version, event_type, occurred_at, actor, run_id, causation_id, raw = row
    payload: JsonValue = json.loads(raw)
    if type(payload) is not dict:
        raise EventValidationError("payload", "stored payload must be a JSON object")
    document: dict[str, JsonValue] = {
        "event_id": event_id,
        "schema_version": version,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "actor": actor,
        "run_id": run_id,
        "causation_id": causation_id,
        "payload": payload,
    }
    return EventRecord(sequence=sequence, envelope=upcast(document))


def _projection_records(
    rows: Iterable[tuple[str, str, int, str]],
) -> tuple[ProjectionRecord, ...]:
    return tuple(
        ProjectionRecord(name, entity_id, version, state_json.encode())
        for name, entity_id, version, state_json in rows
    )


@dataclass(frozen=True, slots=True)
class ReplayVerification:
    matched: bool
    projection_json: bytes
    logical_digest: str


def _projection_json(records: tuple[ProjectionRecord, ...]) -> bytes:
    return canonical_json(
        tuple(
            {
                "entity_id": record.entity_id,
                "projection_name": record.projection_name,
                "state_json": record.state_json.decode(),
                "version": record.version,
            }
            for record in records
        )
    )


def _logical_digest(records: tuple[ProjectionRecord, ...]) -> str:
    protected = tuple(
        record for record in records if record.projection_name not in AUDIT_PROJECTIONS
    )
    return sha256_bytes(_projection_json(protected))
