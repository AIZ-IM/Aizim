from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import cast

from aizim.domain.serialization import JsonValue, canonical_json

from .event_payload import thaw_payload
from .events import EventEnvelope
from .projections import ProjectionRecord, ProjectionReducer
from .publications import PublicationQueueEntry, PublicationQueueState, can_transition
from .store_contracts import EventRecord, ProjectionAuthorityError, _projection_records
from .store_mutations import (
    EventMutation,
    MutationConnection,
    apply_event_mutation,
    revoke_lease_capabilities,
)

type EventFactory = Callable[
    [str, PublicationQueueState, int, str | None, str | None], EventEnvelope
]


def enqueue(
    connection: MutationConnection,
    reducer: ProjectionReducer,
    submitted: EventEnvelope,
    queued: EventEnvelope,
) -> PublicationQueueEntry:
    contribution_id = _contribution_id(submitted)
    if queued.event_type != "ContributionEnqueued" or _contribution_id(queued) != contribution_id:
        raise ValueError("INVALID_CONTRIBUTION_ENQUEUE")
    _begin(connection)
    try:
        existing = connection.execute(
            "SELECT enqueue_sequence,contribution_id,state,state_version,claimed_by,claimed_at "
            "FROM publication_queue WHERE contribution_id=?",
            (contribution_id,),
        ).fetchone()
        if existing is not None:
            if not _same_submission(connection, submitted):
                raise ValueError("DUPLICATE_CONTRIBUTION_MISMATCH")
            _commit(connection)
            return _entry(existing)
        _append(connection, reducer, submitted)
        cursor = connection.execute(
            "INSERT INTO publication_queue("
            "contribution_id,state,state_version,claimed_by,claimed_at) "
            "VALUES(?,?,?,?,?)",
            (contribution_id, PublicationQueueState.QUEUED.value, 0, None, None),
        )
        sequence = cursor.lastrowid
        if sequence is None:
            raise ProjectionAuthorityError("publication_queue", "missing enqueue sequence")
        _append(connection, reducer, queued)
        _commit(connection)
    except BaseException:
        _rollback(connection)
        raise
    return PublicationQueueEntry(
        contribution_id, sequence, PublicationQueueState.QUEUED, 0, None, None
    )


def _same_submission(connection: MutationConnection, submitted: EventEnvelope) -> bool:
    payload = canonical_json(thaw_payload(submitted.payload)).decode()
    row = connection.execute(
        "SELECT run_id FROM events WHERE event_type='ContributionSubmitted' AND payload_json=?",
        (payload,),
    ).fetchone()
    return row is not None and row[0] == submitted.run_id


def claim_next(
    connection: MutationConnection,
    reducer: ProjectionReducer,
    owner_id: str,
    claimed_at: datetime,
    event: EventFactory,
) -> PublicationQueueEntry | None:
    _begin(connection)
    try:
        if _active(connection):
            _commit(connection)
            return None
        row = connection.execute(
            "SELECT enqueue_sequence,contribution_id,state,state_version,claimed_by,claimed_at "
            "FROM publication_queue WHERE state=? "
            "ORDER BY enqueue_sequence,contribution_id LIMIT 1",
            (PublicationQueueState.QUEUED.value,),
        ).fetchone()
        if row is None:
            _commit(connection)
            return None
        current = _entry(row)
        claimed = _update(connection, current, owner_id, claimed_at, PublicationQueueState.STAGED)
        _append(
            connection,
            reducer,
            event(
                claimed.contribution_id,
                claimed.state,
                claimed.state_version,
                _run(connection, claimed.contribution_id),
                None,
            ),
        )
        _commit(connection)
        return claimed
    except BaseException:
        _rollback(connection)
        raise


def advance(
    connection: MutationConnection,
    reducer: ProjectionReducer,
    contribution_id: str,
    owner_id: str,
    target: PublicationQueueState,
    changed_at: datetime,
    event: EventFactory,
) -> PublicationQueueEntry:
    if target is PublicationQueueState.PUBLISHED:
        raise ValueError("PUBLISHED_REQUIRES_PREPARATION")
    _begin(connection)
    try:
        row = connection.execute(
            "SELECT enqueue_sequence,contribution_id,state,state_version,claimed_by,claimed_at "
            "FROM publication_queue WHERE contribution_id=?",
            (contribution_id,),
        ).fetchone()
        if row is None:
            raise ValueError("PROMOTION_NOT_FOUND")
        current = _entry(row)
        if current.claimed_by != owner_id:
            raise ValueError("PROMOTION_CLAIM_DENIED")
        if not can_transition(current.state, target):
            raise ValueError("INVALID_PROMOTION_TRANSITION")
        changed = _update(connection, current, owner_id, changed_at, target)
        _append(
            connection,
            reducer,
            event(
                changed.contribution_id,
                changed.state,
                changed.state_version,
                _run(connection, changed.contribution_id),
                None,
            ),
        )
        _commit(connection)
        return changed
    except BaseException:
        _rollback(connection)
        raise


def _active(connection: MutationConnection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM publication_queue WHERE claimed_by IS NOT NULL AND state IN (?,?,?) LIMIT 1",
        (
            PublicationQueueState.STAGED.value,
            PublicationQueueState.VERIFIED.value,
            PublicationQueueState.MATERIALIZED.value,
        ),
    ).fetchone()
    return row is not None


def _append(
    connection: MutationConnection, reducer: ProjectionReducer, event: EventEnvelope
) -> EventRecord:
    snapshots = _snapshots(connection)
    record = apply_event_mutation(connection, EventMutation(reducer, event, snapshots))
    revoke_lease_capabilities(connection, event)
    return record


def _snapshots(connection: MutationConnection) -> tuple[ProjectionRecord, ...]:
    rows = connection.execute(
        "SELECT projection_name,entity_id,version,state_json FROM projections "
        "ORDER BY projection_name,entity_id"
    ).fetchall()
    return _projection_records(cast(list[tuple[str, str, int, str]], rows))


def _update(
    connection: MutationConnection,
    current: PublicationQueueEntry,
    owner_id: str,
    claimed_at: datetime,
    target: PublicationQueueState,
) -> PublicationQueueEntry:
    version = current.state_version + 1
    cursor = connection.execute(
        "UPDATE publication_queue SET state=?,state_version=?,claimed_by=?,claimed_at=? "
        "WHERE contribution_id=? AND state_version=?",
        (
            target.value,
            version,
            owner_id,
            _timestamp(claimed_at),
            current.contribution_id,
            current.state_version,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("PROMOTION_CLAIM_DENIED")
    return PublicationQueueEntry(
        current.contribution_id, current.enqueue_sequence, target, version, owner_id, claimed_at
    )


def _entry(row: tuple[object, ...]) -> PublicationQueueEntry:
    if len(row) != 6:
        raise ProjectionAuthorityError("publication_queue", "invalid row")
    sequence, contribution_id, state, version, owner, claimed = row
    if (
        type(sequence) is not int
        or type(contribution_id) is not str
        or type(state) is not str
        or type(version) is not int
        or (owner is not None and type(owner) is not str)
        or (claimed is not None and type(claimed) is not str)
    ):
        raise ProjectionAuthorityError("publication_queue", "invalid row")
    try:
        parsed_state = PublicationQueueState(state)
    except ValueError:
        raise ProjectionAuthorityError("publication_queue", "invalid state") from None
    return PublicationQueueEntry(
        contribution_id,
        sequence,
        parsed_state,
        version,
        owner,
        None if claimed is None else _parse_timestamp(claimed),
    )


def _contribution_id(event: EventEnvelope) -> str:
    value = event.payload.get("contribution_id")
    if type(value) is not str or not value:
        raise ValueError("INVALID_CONTRIBUTION_ENQUEUE")
    return value


def _run(connection: MutationConnection, contribution_id: str) -> str | None:
    rows = connection.execute(
        "SELECT run_id,payload_json FROM events "
        "WHERE event_type='ContributionSubmitted' ORDER BY sequence"
    ).fetchall()
    for run_id, raw in rows:
        payload: JsonValue = json.loads(cast(str, raw))
        if type(payload) is dict and payload.get("contribution_id") == contribution_id:
            return run_id if type(run_id) is str else None
    raise ValueError("PROMOTION_NOT_FOUND")


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")


def _begin(connection: MutationConnection) -> None:
    connection.execute("BEGIN IMMEDIATE")


def _commit(connection: MutationConnection) -> None:
    connection.execute("COMMIT")


def _rollback(connection: MutationConnection) -> None:
    with suppress(Exception):
        connection.execute("ROLLBACK")
