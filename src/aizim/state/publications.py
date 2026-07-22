from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class PublicationQueueState(StrEnum):
    QUEUED = "queued"
    STAGED = "staged"
    VERIFIED = "verified"
    MATERIALIZED = "materialized"
    PUBLISHED = "published"
    QUARANTINED = "quarantined"


_NEXT = {
    PublicationQueueState.STAGED: frozenset(
        {PublicationQueueState.VERIFIED, PublicationQueueState.QUARANTINED}
    ),
    PublicationQueueState.VERIFIED: frozenset(
        {PublicationQueueState.MATERIALIZED, PublicationQueueState.QUARANTINED}
    ),
    PublicationQueueState.MATERIALIZED: frozenset(
        {PublicationQueueState.PUBLISHED, PublicationQueueState.QUARANTINED}
    ),
}


@dataclass(frozen=True, slots=True)
class PublicationQueueEntry:
    contribution_id: str
    enqueue_sequence: int
    state: PublicationQueueState
    state_version: int
    claimed_by: str | None
    claimed_at: datetime | None

    def __post_init__(self) -> None:
        if type(self.contribution_id) is not str or not self.contribution_id:
            raise ValueError("INVALID_PUBLICATION_ENTRY")
        if type(self.enqueue_sequence) is not int or self.enqueue_sequence < 1:
            raise ValueError("INVALID_PUBLICATION_ENTRY")
        if type(self.state) is not PublicationQueueState:
            raise ValueError("INVALID_PUBLICATION_ENTRY")
        if type(self.state_version) is not int or self.state_version < 0:
            raise ValueError("INVALID_PUBLICATION_ENTRY")
        if self.claimed_by is not None and (
            type(self.claimed_by) is not str or not self.claimed_by
        ):
            raise ValueError("INVALID_PUBLICATION_ENTRY")
        if self.claimed_at is not None and (
            type(self.claimed_at) is not datetime
            or self.claimed_at.tzinfo is None
            or self.claimed_at.utcoffset() != UTC.utcoffset(self.claimed_at)
        ):
            raise ValueError("INVALID_PUBLICATION_ENTRY")


def can_transition(current: PublicationQueueState, target: PublicationQueueState) -> bool:
    return target in _NEXT.get(current, frozenset())
