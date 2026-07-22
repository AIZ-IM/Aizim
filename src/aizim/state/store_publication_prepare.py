from __future__ import annotations

import json
from datetime import datetime

from aizim.domain.serialization import canonical_json

from .event_payload import thaw_payload
from .events import EventEnvelope
from .projections import ProjectionReducer
from .publications import PublicationQueueEntry, PublicationQueueState
from .store_mutations import MutationConnection
from .store_publication import (
    EventFactory,
    _append,
    _begin,
    _commit,
    _entry,
    _rollback,
    _run,
    _update,
)


def prepare(
    connection: MutationConnection,
    reducer: ProjectionReducer,
    contribution_id: str,
    owner_id: str,
    changed_at: datetime,
    prepared: EventEnvelope,
) -> PublicationQueueEntry:
    _begin(connection)
    try:
        current = _current(connection, contribution_id)
        if (
            current.claimed_by != owner_id
            or current.state is not PublicationQueueState.MATERIALIZED
        ):
            raise ValueError("INVALID_PROMOTION_TRANSITION")
        _prepared_payload(prepared, contribution_id)
        existing = next(
            (
                row[0]
                for row in connection.execute(
                    "SELECT payload_json FROM events WHERE event_type='PromotionPrepared'"
                ).fetchall()
                if type(row[0]) is str
                and json.loads(row[0]).get("contribution_id") == contribution_id
            ),
            None,
        )
        if existing is None:
            _append(connection, reducer, prepared)
        elif existing != canonical_json(thaw_payload(prepared.payload)).decode():
            raise ValueError("PROMOTION_PREPARATION_MISMATCH")
        _commit(connection)
        return current
    except BaseException:
        _rollback(connection)
        raise


def require_prepared(
    connection: MutationConnection,
    contribution_id: str,
    declaration: EventEnvelope,
    delta: EventEnvelope,
) -> None:
    expected = {
        "contribution_id": contribution_id,
        "module": declaration.payload.get("module"),
        "content_hash": declaration.payload.get("content_hash"),
        "base_epoch": delta.payload.get("base_epoch"),
        "knowledge_epoch": delta.payload.get("knowledge_epoch"),
        "declaration_id": declaration.payload.get("declaration_id"),
        "delta_id": delta.payload.get("delta_id"),
    }
    rows = connection.execute(
        "SELECT payload_json FROM events WHERE event_type='PromotionPrepared'"
    ).fetchall()
    if not any(type(row[0]) is str and json.loads(row[0]) == expected for row in rows):
        raise ValueError("PROMOTION_NOT_PREPARED")


def rebase(
    connection: MutationConnection,
    reducer: ProjectionReducer,
    contribution_id: str,
    owner_id: str,
    changed_at: datetime,
    submitted: EventEnvelope,
    queued: EventEnvelope,
    rebased: EventEnvelope,
    event: EventFactory,
) -> tuple[PublicationQueueEntry, PublicationQueueEntry]:
    _begin(connection)
    try:
        current = _current(connection, contribution_id)
        new_id = _contribution_id(submitted)
        if (
            current.claimed_by != owner_id
            or current.state
            not in {
                PublicationQueueState.STAGED,
                PublicationQueueState.VERIFIED,
                PublicationQueueState.MATERIALIZED,
            }
            or queued.event_type != "ContributionEnqueued"
            or _contribution_id(queued) != new_id
            or rebased.event_type != "ContributionRebased"
            or rebased.payload.get("contribution_id") != new_id
            or rebased.payload.get("source_contribution_id") != contribution_id
        ):
            raise ValueError("INVALID_PROMOTION_REBASE")
        if (
            connection.execute(
                "SELECT 1 FROM publication_queue WHERE contribution_id=?", (new_id,)
            ).fetchone()
            is not None
        ):
            raise ValueError("DUPLICATE_CONTRIBUTION_MISMATCH")
        quarantined = _update(
            connection, current, owner_id, changed_at, PublicationQueueState.QUARANTINED
        )
        _append(
            connection,
            reducer,
            event(
                contribution_id,
                quarantined.state,
                quarantined.state_version,
                _run(connection, contribution_id),
                None,
            ),
        )
        _append(connection, reducer, submitted)
        cursor = connection.execute(
            "INSERT INTO publication_queue("
            "contribution_id,state,state_version,claimed_by,claimed_at"
            ") VALUES(?,?,?,?,?)",
            (new_id, PublicationQueueState.QUEUED.value, 0, None, None),
        )
        sequence = cursor.lastrowid
        if type(sequence) is not int:
            raise ValueError("INVALID_PROMOTION_REBASE")
        _append(connection, reducer, queued)
        _append(connection, reducer, rebased)
        _commit(connection)
        return quarantined, PublicationQueueEntry(
            new_id, sequence, PublicationQueueState.QUEUED, 0, None, None
        )
    except BaseException:
        _rollback(connection)
        raise


def _current(connection: MutationConnection, contribution_id: str) -> PublicationQueueEntry:
    row = connection.execute(
        "SELECT enqueue_sequence,contribution_id,state,state_version,claimed_by,claimed_at "
        "FROM publication_queue WHERE contribution_id=?",
        (contribution_id,),
    ).fetchone()
    if row is None:
        raise ValueError("PROMOTION_NOT_FOUND")
    return _entry(row)


def _contribution_id(event: EventEnvelope) -> str:
    value = event.payload.get("contribution_id")
    if type(value) is not str or not value:
        raise ValueError("INVALID_PROMOTION_REBASE")
    return value


def _prepared_payload(event: EventEnvelope, contribution_id: str) -> None:
    if (
        event.event_type != "PromotionPrepared"
        or event.payload.get("contribution_id") != contribution_id
    ):
        raise ValueError("INVALID_PROMOTION_PREPARATION")
