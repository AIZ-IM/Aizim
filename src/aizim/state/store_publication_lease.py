from __future__ import annotations

from datetime import datetime

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


def heartbeat(
    connection: MutationConnection,
    reducer: ProjectionReducer,
    contribution_id: str,
    owner_id: str,
    changed_at: datetime,
    event: EventFactory,
) -> PublicationQueueEntry:
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
        if current.claimed_by != owner_id or current.state not in {
            PublicationQueueState.STAGED,
            PublicationQueueState.VERIFIED,
            PublicationQueueState.MATERIALIZED,
        }:
            raise ValueError("PROMOTION_CLAIM_DENIED")
        refreshed = _update(connection, current, owner_id, changed_at, current.state)
        _append(
            connection,
            reducer,
            event(
                refreshed.contribution_id,
                refreshed.state,
                refreshed.state_version,
                _run(connection, refreshed.contribution_id),
                None,
            ),
        )
        _commit(connection)
        return refreshed
    except BaseException:
        _rollback(connection)
        raise
