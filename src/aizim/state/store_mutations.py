from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from aizim.domain.serialization import canonical_json

from .capabilities import CapabilityRecord, canonical_timestamp, capability_row
from .events import EventEnvelope, event_as_dict
from .projections import PROJECTION_NAMES, ProjectionRecord, ProjectionReducer
from .store_contracts import EventRecord, ProjectionAuthorityError

type SqlValue = str | int | None
type SqlParameters = tuple[SqlValue, ...]


class MutationCursor(Protocol):
    @property
    def lastrowid(self) -> int | None: ...

    @property
    def rowcount(self) -> int: ...

    def fetchall(self) -> list[tuple[object, ...]]: ...

    def fetchone(self) -> tuple[object, ...] | None: ...


class MutationConnection(Protocol):
    def execute(self, sql: str, parameters: SqlParameters = (), /) -> MutationCursor: ...


@dataclass(frozen=True, slots=True)
class EventMutation:
    reducer: ProjectionReducer
    event: EventEnvelope
    snapshots: tuple[ProjectionRecord, ...]


def apply_event_mutation(connection: MutationConnection, mutation: EventMutation) -> EventRecord:
    document = event_as_dict(mutation.event)
    cursor = connection.execute(
        "INSERT INTO events(event_id,schema_version,event_type,occurred_at,actor,"
        "run_id,causation_id,payload_json) VALUES(?,?,?,?,?,?,?,?)",
        (
            document["event_id"],
            document["schema_version"],
            document["event_type"],
            document["occurred_at"],
            document["actor"],
            document["run_id"],
            document["causation_id"],
            canonical_json(document["payload"]).decode(),
        ),
    )
    sequence = cursor.lastrowid
    if sequence is None:
        raise ProjectionAuthorityError("events", "insert did not allocate a sequence")
    for change in mutation.reducer(mutation.snapshots, mutation.event):
        if change.projection_name not in PROJECTION_NAMES:
            raise ProjectionAuthorityError(change.projection_name, "name is not registered")
        connection.execute(
            "INSERT INTO projections(projection_name,entity_id,version,state_json) "
            "VALUES(?,?,?,?) ON CONFLICT(projection_name,entity_id) DO UPDATE SET "
            "version=excluded.version,state_json=excluded.state_json",
            (
                change.projection_name,
                change.entity_id,
                change.version,
                change.state_json.decode(),
            ),
        )
    return EventRecord(sequence=sequence, envelope=mutation.event)


def insert_capability(connection: MutationConnection, record: CapabilityRecord) -> None:
    connection.execute(
        "INSERT INTO capability_tokens(token_hash,run_id,worker_id,role,lease_id,"
        "operations_json,expires_at,revoked_at) VALUES(?,?,?,?,?,?,?,?)",
        capability_row(record),
    )


def register_artifact(connection: MutationConnection, event: EventEnvelope) -> None:
    if event.event_type != "ArtifactRegistered":
        return
    payload = event.payload
    run_id = _artifact_text(event.run_id)
    artifact_name = _artifact_text(payload.get("artifact_name"))
    content_hash = _artifact_text(payload.get("content_hash"))
    relative_path = _artifact_text(payload.get("relative_path"))
    media_type = _artifact_text(payload.get("media_type"))
    byte_length = _artifact_integer(payload.get("byte_length"))
    connection.execute(
        "INSERT OR IGNORE INTO artifacts(content_hash,run_id,relative_path,media_type,byte_length) "
        "VALUES(?,?,?,?,?)",
        (content_hash, run_id, relative_path, media_type, byte_length),
    )
    row = connection.execute(
        "SELECT media_type,byte_length FROM artifacts WHERE content_hash=?",
        (content_hash,),
    ).fetchone()
    if row != (media_type, byte_length):
        raise ProjectionAuthorityError(
            "artifacts", "artifact registration conflicts with stored hash"
        )
    association = (run_id, artifact_name, relative_path, content_hash, media_type, byte_length)
    connection.execute(
        "INSERT OR IGNORE INTO artifact_associations("
        "run_id,artifact_name,relative_path,content_hash,media_type,byte_length) "
        "VALUES(?,?,?,?,?,?)",
        association,
    )
    stored = connection.execute(
        "SELECT run_id,artifact_name,relative_path,content_hash,media_type,byte_length "
        "FROM artifact_associations WHERE run_id=? AND relative_path=?",
        (run_id, relative_path),
    ).fetchone()
    if stored != association:
        raise ProjectionAuthorityError("artifacts", "run artifact registration conflicts")


def _artifact_text(value: object) -> str:
    if type(value) is not str:
        raise ProjectionAuthorityError("artifacts", "artifact registration is incomplete")
    return value


def _artifact_integer(value: object) -> int:
    if type(value) is not int:
        raise ProjectionAuthorityError("artifacts", "artifact registration is incomplete")
    return value


def revoke_capability(
    connection: MutationConnection, token_hash: str, revoked_at: datetime
) -> bool:
    cursor = connection.execute(
        "UPDATE capability_tokens SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
        (canonical_timestamp(revoked_at), token_hash),
    )
    return cursor.rowcount > 0


def revoke_lease_capabilities(connection: MutationConnection, event: EventEnvelope) -> None:
    if event.event_type not in {"LeaseReleased", "LeaseRecovered"}:
        return
    if event.run_id is None:
        raise ProjectionAuthorityError("run_id", "lease terminal event requires a run")
    lease_id = event.payload.get("lease_id")
    if type(lease_id) is not str:
        raise ProjectionAuthorityError("lease_id", "lease terminal event requires a lease")
    connection.execute(
        "UPDATE capability_tokens SET revoked_at=? "
        "WHERE run_id=? AND lease_id=? AND revoked_at IS NULL",
        (canonical_timestamp(event.occurred_at), event.run_id, lease_id),
    )
