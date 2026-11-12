from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta

from aizim.knowledge import (
    ArtifactStore,
    KnowledgeReader,
    PromotionOutcome,
    PromotionService,
    RuntimePromotionVerifier,
)
from aizim.state import PublicationQueueState, StateService

from .knowledge_stream import KnowledgeStream

# A live owner heartbeats every second; a claim this old belongs to a consumer that stopped.
STALE_PROMOTION_CLAIM = timedelta(minutes=1)
# How often a long-lived consumer looks for promotions that no submission announced.
_RECOVERY_POLL_SECONDS = 15.0


class PromotionConsumerError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class PromotionConsumer:
    def __init__(
        self,
        state: StateService,
        artifacts: ArtifactStore,
        verifier: RuntimePromotionVerifier,
        knowledge: KnowledgeStream,
        *,
        continue_on_failure: bool = False,
    ) -> None:
        self._continue_on_failure = continue_on_failure
        self._state, self._artifacts = state, artifacts
        self._verifier, self._knowledge = verifier, knowledge
        self._wake, self._published, self._failed = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self.failure: PromotionOutcome | None = None

    def notify_submission(self) -> None:
        self._wake.set()

    async def run(self, stop: asyncio.Event) -> None:
        # Drain persisted work immediately, including submissions made by an earlier process.
        while not stop.is_set():
            self._wake.clear()
            while not stop.is_set():
                outcome = await self._next_outcome()
                if outcome is None:
                    break
                if outcome.state is not PublicationQueueState.PUBLISHED:
                    if self._continue_on_failure:
                        self._published.set()
                        continue
                    self.failure = outcome
                    self._published.set()
                    self._failed.set()
                    return
                self._published.set()
                await self._knowledge.notify_published()
            if not stop.is_set():
                await self._next_wake()

    async def _next_wake(self) -> None:
        # Every consumer also finds claims whose owner stopped heartbeating, even if no
        # submission in this process announced them. Failure handling is independent.
        with suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), _RECOVERY_POLL_SECONDS)

    async def _next_outcome(self) -> PromotionOutcome | None:
        service = self._service()
        outcome = await service.promote_next()
        if outcome is None:
            # An abandoned claim blocks every later promotion until it is taken over.
            outcome = await service.recover_next(STALE_PROMOTION_CLAIM)
        return outcome

    async def wait_for_epoch(self, epoch: int) -> None:
        while KnowledgeReader(self._state).read(epoch - 1) == ():
            self._published.clear()
            if self.failure is not None:
                raise self._failure_error()
            await self._published.wait()

    async def wait_for_failure(self) -> None:
        await self._failed.wait()
        raise self._failure_error()

    def _failure_error(self) -> PromotionConsumerError:
        outcome = self.failure
        if outcome is None:
            return PromotionConsumerError("PROMOTION_FAILED")
        if outcome.rebased_contribution_id is not None:
            return PromotionConsumerError("EPOCH_MISMATCH")
        reason = next(
            (
                record.envelope.payload.get("reason_code")
                for record in reversed(self._state.query_events())
                if record.envelope.event_type == "PromotionFailed"
                and record.envelope.payload.get("contribution_id") == outcome.contribution_id
            ),
            None,
        )
        return PromotionConsumerError(reason if type(reason) is str else "PROMOTION_FAILED")

    def _service(self) -> PromotionService:
        imports = (
            *self._verifier.allowed_imports,
            *(item.module for item in KnowledgeReader(self._state).read(0)),
        )
        return PromotionService(
            self._state,
            self._artifacts,
            self._verifier,
            "shared-promotion-consumer",
            self._verifier,
            tuple(dict.fromkeys(imports)),
            namespace=self._verifier.namespace,
        )
