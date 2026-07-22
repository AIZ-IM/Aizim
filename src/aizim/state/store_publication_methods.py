from __future__ import annotations

from datetime import datetime
from typing import cast

from .events import EventEnvelope
from .projections import ProjectionReducer
from .publications import PublicationQueueEntry, PublicationQueueState, can_transition
from .store_mutations import MutationConnection
from .store_publication import (
    EventFactory,
    _append,
    _begin,
    _commit,
    _entry,
    _rollback,
    _run,
    _timestamp,
    _update,
    advance,
    claim_next,
    enqueue,
)
from .store_publication_lease import heartbeat
from .store_publication_prepare import prepare, rebase, require_prepared


class PublicationStoreMethods:
    def enqueue_contribution(
        self, submitted: EventEnvelope, queued: EventEnvelope
    ) -> PublicationQueueEntry:
        return enqueue(
            self._publication_connection(), self._publication_reducer(), submitted, queued
        )

    def claim_next_promotion(
        self, owner_id: str, claimed_at: datetime, event: EventFactory
    ) -> PublicationQueueEntry | None:
        return claim_next(
            self._publication_connection(), self._publication_reducer(), owner_id, claimed_at, event
        )

    def advance_promotion(
        self,
        contribution_id: str,
        owner_id: str,
        target: PublicationQueueState,
        changed_at: datetime,
        event: EventFactory,
    ) -> PublicationQueueEntry:
        return advance(
            self._publication_connection(),
            self._publication_reducer(),
            contribution_id,
            owner_id,
            target,
            changed_at,
            event,
        )

    def heartbeat_promotion(
        self,
        contribution_id: str,
        owner_id: str,
        changed_at: datetime,
        event: EventFactory,
    ) -> PublicationQueueEntry:
        return heartbeat(
            self._publication_connection(),
            self._publication_reducer(),
            contribution_id,
            owner_id,
            changed_at,
            event,
        )

    def prepare_promotion(
        self,
        contribution_id: str,
        owner_id: str,
        changed_at: datetime,
        prepared: EventEnvelope,
    ) -> PublicationQueueEntry:
        return prepare(
            self._publication_connection(),
            self._publication_reducer(),
            contribution_id,
            owner_id,
            changed_at,
            prepared,
        )

    def publish_promotion(
        self,
        contribution_id: str,
        owner_id: str,
        changed_at: datetime,
        declaration: EventEnvelope,
        delta: EventEnvelope,
        event: EventFactory,
    ) -> PublicationQueueEntry:
        connection, reducer = self._publication_connection(), self._publication_reducer()
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
            if current.claimed_by != owner_id or not can_transition(
                current.state, PublicationQueueState.PUBLISHED
            ):
                raise ValueError("INVALID_PROMOTION_TRANSITION")
            require_prepared(connection, contribution_id, declaration, delta)
            published = _update(
                connection, current, owner_id, changed_at, PublicationQueueState.PUBLISHED
            )
            _append(connection, reducer, declaration)
            _append(connection, reducer, delta)
            _append(
                connection,
                reducer,
                event(
                    published.contribution_id,
                    published.state,
                    published.state_version,
                    declaration.run_id,
                    None,
                ),
            )
            _commit(connection)
            return published
        except BaseException:
            _rollback(connection)
            raise

    def fail_promotion(
        self,
        contribution_id: str,
        owner_id: str,
        changed_at: datetime,
        failure: EventEnvelope,
        event: EventFactory,
    ) -> PublicationQueueEntry:
        connection, reducer = self._publication_connection(), self._publication_reducer()
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
            if current.claimed_by != owner_id or not can_transition(
                current.state, PublicationQueueState.QUARANTINED
            ):
                raise ValueError("INVALID_PROMOTION_TRANSITION")
            quarantined = _update(
                connection, current, owner_id, changed_at, PublicationQueueState.QUARANTINED
            )
            _append(connection, reducer, failure)
            _append(
                connection,
                reducer,
                event(
                    quarantined.contribution_id,
                    quarantined.state,
                    quarantined.state_version,
                    failure.run_id,
                    None,
                ),
            )
            _commit(connection)
            return quarantined
        except BaseException:
            _rollback(connection)
            raise

    def recover_expired_promotion(
        self, owner_id: str, now: datetime, stale_before: datetime, event: EventFactory
    ) -> PublicationQueueEntry | None:
        connection, reducer = self._publication_connection(), self._publication_reducer()
        _begin(connection)
        try:
            row = connection.execute(
                "SELECT enqueue_sequence,contribution_id,state,state_version,claimed_by,claimed_at "
                "FROM publication_queue WHERE claimed_by IS NOT NULL AND claimed_at < ? "
                "AND state IN (?,?,?) ORDER BY enqueue_sequence,contribution_id LIMIT 1",
                (
                    _timestamp(stale_before),
                    PublicationQueueState.STAGED.value,
                    PublicationQueueState.VERIFIED.value,
                    PublicationQueueState.MATERIALIZED.value,
                ),
            ).fetchone()
            if row is None:
                _commit(connection)
                return None
            current = _entry(row)
            recovered = _update(connection, current, owner_id, now, current.state)
            _append(
                connection,
                reducer,
                event(
                    recovered.contribution_id,
                    recovered.state,
                    recovered.state_version,
                    _run(connection, recovered.contribution_id),
                    current.claimed_by,
                ),
            )
            _commit(connection)
            return recovered
        except BaseException:
            _rollback(connection)
            raise

    def rebase_promotion(
        self,
        contribution_id: str,
        owner_id: str,
        changed_at: datetime,
        submitted: EventEnvelope,
        queued: EventEnvelope,
        rebased: EventEnvelope,
        event: EventFactory,
    ) -> tuple[PublicationQueueEntry, PublicationQueueEntry]:
        return rebase(
            self._publication_connection(),
            self._publication_reducer(),
            contribution_id,
            owner_id,
            changed_at,
            submitted,
            queued,
            rebased,
            event,
        )

    def _publication_connection(self) -> MutationConnection:
        return cast(MutationConnection, object.__getattribute__(self, "_connection"))

    def _publication_reducer(self) -> ProjectionReducer:
        return cast(ProjectionReducer, object.__getattribute__(self, "_reducer"))
