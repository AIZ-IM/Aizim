from __future__ import annotations

import asyncio

from aizim.knowledge import (
    ArtifactStore,
    KnowledgeReader,
    PromotionOutcome,
    PromotionService,
    RuntimePromotionVerifier,
)
from aizim.state import PublicationQueueState, StateService

from .knowledge_stream import KnowledgeStream


class PromotionConsumer:
    def __init__(
        self,
        state: StateService,
        artifacts: ArtifactStore,
        verifier: RuntimePromotionVerifier,
        knowledge: KnowledgeStream,
    ) -> None:
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
        while not stop.is_set():
            await self._wake.wait()
            self._wake.clear()
            while not stop.is_set():
                outcome = await self._service().promote_next()
                if outcome is None:
                    break
                if outcome.state is not PublicationQueueState.PUBLISHED:
                    self.failure = outcome
                    self._published.set()
                    self._failed.set()
                    return
                self._published.set()
                await self._knowledge.notify_published()

    async def wait_for_epoch(self, epoch: int) -> None:
        while KnowledgeReader(self._state).read(epoch - 1) == ():
            self._published.clear()
            if self.failure is not None:
                raise RuntimeError("PROMOTION_FAILED")
            await self._published.wait()

    async def wait_for_failure(self) -> None:
        await self._failed.wait()
        raise RuntimeError("PROMOTION_FAILED")

    def _service(self) -> PromotionService:
        imports = ("Std", *(item.module for item in KnowledgeReader(self._state).read(0)))
        return PromotionService(
            self._state,
            self._artifacts,
            self._verifier,
            "shared-promotion-consumer",
            self._verifier,
            tuple(dict.fromkeys(imports)),
        )
