from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from aizim.domain.serialization import JsonValue

from .events import EventEnvelope, EventValidationError
from .operations import AppendEventCommand
from .publications import PublicationQueueEntry, PublicationQueueState
from .store_publication import EventFactory


class PromotionStore(Protocol):
    def enqueue_contribution(
        self, submitted: EventEnvelope, queued: EventEnvelope
    ) -> PublicationQueueEntry: ...

    def claim_next_promotion(
        self, owner_id: str, claimed_at: datetime, event: EventFactory
    ) -> PublicationQueueEntry | None: ...

    def advance_promotion(
        self,
        contribution_id: str,
        owner_id: str,
        target: PublicationQueueState,
        changed_at: datetime,
        event: EventFactory,
    ) -> PublicationQueueEntry: ...

    def heartbeat_promotion(
        self,
        contribution_id: str,
        owner_id: str,
        changed_at: datetime,
        event: EventFactory,
    ) -> PublicationQueueEntry: ...

    def prepare_promotion(
        self,
        contribution_id: str,
        owner_id: str,
        changed_at: datetime,
        prepared: EventEnvelope,
    ) -> PublicationQueueEntry: ...

    def publish_promotion(
        self,
        contribution_id: str,
        owner_id: str,
        changed_at: datetime,
        declaration: EventEnvelope,
        delta: EventEnvelope,
        event: EventFactory,
    ) -> PublicationQueueEntry: ...

    def fail_promotion(
        self,
        contribution_id: str,
        owner_id: str,
        changed_at: datetime,
        failure: EventEnvelope,
        event: EventFactory,
    ) -> PublicationQueueEntry: ...

    def recover_expired_promotion(
        self, owner_id: str, now: datetime, stale_before: datetime, event: EventFactory
    ) -> PublicationQueueEntry | None: ...

    def rebase_promotion(
        self,
        contribution_id: str,
        owner_id: str,
        changed_at: datetime,
        submitted: EventEnvelope,
        queued: EventEnvelope,
        rebased: EventEnvelope,
        event: EventFactory,
    ) -> tuple[PublicationQueueEntry, PublicationQueueEntry]: ...


class PromotionStateMethods:
    def _event(self, command: AppendEventCommand) -> EventEnvelope:
        raise NotImplementedError

    def _promotion_now(self) -> datetime:
        raise NotImplementedError

    def _promotion_store(self) -> PromotionStore:
        raise NotImplementedError

    def enqueue_contribution(self, submitted: AppendEventCommand) -> PublicationQueueEntry:
        event = self._event(submitted)
        if event.event_type != "ContributionSubmitted":
            raise EventValidationError("event_type", "must submit a contribution")
        contribution_id = event.payload.get("contribution_id")
        if type(contribution_id) is not str:
            raise EventValidationError("contribution_id", "must be a non-empty string")
        queued = self._event(
            AppendEventCommand(
                "ContributionEnqueued",
                "contribution_service",
                event.run_id,
                event.event_id,
                {"contribution_id": contribution_id, "state": "queued"},
            )
        )
        return self._promotion_store().enqueue_contribution(event, queued)

    def claim_next_promotion(self, owner_id: str) -> PublicationQueueEntry | None:
        return self._promotion_store().claim_next_promotion(
            owner_id, self._promotion_now(), self._promotion_event
        )

    def advance_promotion(
        self, contribution_id: str, owner_id: str, target: PublicationQueueState
    ) -> PublicationQueueEntry:
        return self._promotion_store().advance_promotion(
            contribution_id, owner_id, target, self._promotion_now(), self._promotion_event
        )

    def heartbeat_promotion(self, contribution_id: str, owner_id: str) -> PublicationQueueEntry:
        return self._promotion_store().heartbeat_promotion(
            contribution_id, owner_id, self._promotion_now(), self._promotion_event
        )

    def prepare_promotion(
        self, contribution_id: str, owner_id: str, prepared: AppendEventCommand
    ) -> PublicationQueueEntry:
        prepared_event = self._event(prepared)
        if prepared_event.event_type != "PromotionPrepared":
            raise EventValidationError("event_type", "must prepare a promotion")
        if prepared_event.payload.get("contribution_id") != contribution_id:
            raise EventValidationError("contribution_id", "must match the promotion")
        return self._promotion_store().prepare_promotion(
            contribution_id, owner_id, self._promotion_now(), prepared_event
        )

    def publish_promotion(
        self,
        contribution_id: str,
        owner_id: str,
        declaration: AppendEventCommand,
        delta: AppendEventCommand,
    ) -> PublicationQueueEntry:
        declaration_event, delta_event = self._event(declaration), self._event(delta)
        if declaration_event.event_type != "DeclarationPublished":
            raise EventValidationError("event_type", "must publish a declaration")
        if delta_event.event_type != "KnowledgeDeltaPublished":
            raise EventValidationError("event_type", "must publish a knowledge delta")
        if declaration_event.payload.get("contribution_id") != contribution_id:
            raise EventValidationError("contribution_id", "must match the declaration")
        if delta_event.payload.get("contribution_id") != contribution_id:
            raise EventValidationError("contribution_id", "must match the delta")
        return self._promotion_store().publish_promotion(
            contribution_id,
            owner_id,
            self._promotion_now(),
            declaration_event,
            delta_event,
            self._promotion_event,
        )

    def fail_promotion(
        self, contribution_id: str, owner_id: str, failure: AppendEventCommand
    ) -> PublicationQueueEntry:
        failure_event = self._event(failure)
        if failure_event.event_type != "PromotionFailed":
            raise EventValidationError("event_type", "must record a promotion failure")
        if failure_event.payload.get("contribution_id") != contribution_id:
            raise EventValidationError("contribution_id", "must match the promotion failure")
        return self._promotion_store().fail_promotion(
            contribution_id,
            owner_id,
            self._promotion_now(),
            failure_event,
            self._promotion_event,
        )

    def recover_expired_promotion(
        self, owner_id: str, claim_timeout: timedelta
    ) -> PublicationQueueEntry | None:
        if type(claim_timeout) is not timedelta or claim_timeout <= timedelta(0):
            raise EventValidationError("claim_timeout", "must be a positive duration")
        now = self._promotion_now()
        return self._promotion_store().recover_expired_promotion(
            owner_id, now, now - claim_timeout, self._promotion_event
        )

    def rebase_promotion(
        self,
        contribution_id: str,
        owner_id: str,
        submitted: AppendEventCommand,
        rebased: AppendEventCommand,
    ) -> tuple[PublicationQueueEntry, PublicationQueueEntry]:
        submitted_event, rebased_event = self._event(submitted), self._event(rebased)
        if submitted_event.event_type != "ContributionSubmitted":
            raise EventValidationError("event_type", "must submit a rebased contribution")
        replacement = submitted_event.payload.get("contribution_id")
        if type(replacement) is not str or not replacement:
            raise EventValidationError("contribution_id", "must be a non-empty string")
        queued_event = self._event(
            AppendEventCommand(
                "ContributionEnqueued",
                "promotion_service",
                submitted_event.run_id,
                submitted_event.event_id,
                {"contribution_id": replacement, "state": "queued"},
            )
        )
        return self._promotion_store().rebase_promotion(
            contribution_id,
            owner_id,
            self._promotion_now(),
            submitted_event,
            queued_event,
            rebased_event,
            self._promotion_event,
        )

    def _promotion_event(
        self,
        contribution_id: str,
        state: PublicationQueueState,
        state_version: int,
        run_id: str | None,
        former_owner: str | None,
    ) -> EventEnvelope:
        payload: dict[str, JsonValue] = {
            "contribution_id": contribution_id,
            "state": state.value,
            "state_version": state_version,
        }
        if former_owner is not None:
            payload["former_owner"] = former_owner
        return self._event(
            AppendEventCommand(
                "PromotionStateChanged",
                "promotion_service",
                run_id,
                None,
                payload,
            )
        )
