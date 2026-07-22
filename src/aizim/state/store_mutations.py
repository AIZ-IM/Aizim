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
