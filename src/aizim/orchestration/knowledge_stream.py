from __future__ import annotations

import asyncio

from aizim.knowledge import KnowledgeReader, PublishedKnowledgeDelta
from aizim.state import StateService


class KnowledgeStream:
    def __init__(self, state: StateService) -> None:
        if type(state) is not StateService:
            raise ValueError("INVALID_KNOWLEDGE_STREAM")
        self._state = state
        self._reader = KnowledgeReader(state)
        self._condition = asyncio.Condition()

    async def read(
        self,
        run_id: str,
        worker_id: str,
        after_knowledge_epoch: int,
        *,
        wait: bool = False,
    ) -> tuple[PublishedKnowledgeDelta, ...]:
        if not _request(run_id, worker_id, after_knowledge_epoch, wait):
            raise ValueError("INVALID_KNOWLEDGE_REQUEST")
        while True:
            deltas = self._reader.read(after_knowledge_epoch)
            if deltas:
                for delta in deltas:
                    self._reader.acknowledge(run_id, worker_id, delta.delta_id)
                return deltas
            if not wait:
                return ()
            async with self._condition:
                if self._reader.read(after_knowledge_epoch):
                    continue
                await self._condition.wait()

    async def notify_published(self) -> None:
        async with self._condition:
            self._condition.notify_all()

    def last_acknowledged(self, run_id: str, worker_id: str) -> str | None:
        if not _request(run_id, worker_id, 0, False):
            raise ValueError("INVALID_KNOWLEDGE_REQUEST")
        last: str | None = None
        for record in self._state.query_events(run_id):
            event = record.envelope
            if (
                event.event_type == "KnowledgeDeltaAcknowledged"
                and event.payload.get("worker_id") == worker_id
            ):
                delta_id = event.payload.get("delta_id")
                if type(delta_id) is str and delta_id:
                    last = delta_id
        return last


def _request(run_id: str, worker_id: str, epoch: int, wait: bool) -> bool:
    return (
        type(run_id) is str
        and bool(run_id)
        and type(worker_id) is str
        and bool(worker_id)
        and type(epoch) is int
        and epoch >= 0
        and type(wait) is bool
    )
