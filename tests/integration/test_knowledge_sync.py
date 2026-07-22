from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aizim.domain.serialization import JsonValue
from aizim.orchestration.knowledge_stream import KnowledgeStream
from aizim.state import AppendEventCommand, StateService, StateServiceConfig


def _delta(
    delta_id: str, previous_base: str, previous_epoch: int, base: str, epoch: int
) -> dict[str, JsonValue]:
    return {
        "delta_id": delta_id,
        "base_epoch": base,
        "knowledge_epoch": epoch,
        "declaration_id": f"declaration-{epoch}",
        "contribution_id": f"contribution-{epoch}",
        "previous_base_epoch": previous_base,
        "previous_knowledge_epoch": previous_epoch,
        "fully_qualified_name": f"AizimSmoke.Research.knowledge_{epoch}",
        "complete_type": "True",
        "module": f"AizimSmoke.Research.K{epoch}",
        "dependencies": [],
        "assumptions": [],
        "axioms": [],
        "evidence_links": [],
        "publication_sequence": epoch,
    }


def _initialized(tmp_path: Path) -> StateService:
    state = StateService(StateServiceConfig(tmp_path, "knowledge-sync"))
    state.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {"project_id": "project", "base_epoch": "a" * 64, "knowledge_epoch": 0},
        )
    )
    return state


@pytest.mark.asyncio
async def test_knowledge_waits_for_durable_delta_then_acknowledges_it(tmp_path: Path) -> None:
    state = _initialized(tmp_path)
    stream = KnowledgeStream(state)
    try:
        waiting = asyncio.create_task(stream.read("run-1", "worker-b", 0, wait=True))
        await asyncio.sleep(0)
        assert not waiting.done()
        state.append_event(
            AppendEventCommand(
                "KnowledgeDeltaPublished",
                "promotion",
                "run-1",
                None,
                _delta("delta-1", "a" * 64, 0, "b" * 64, 1),
            )
        )
        await stream.notify_published()

        deltas = await waiting

        assert [item.delta_id for item in deltas] == ["delta-1"]
        assert stream.last_acknowledged("run-1", "worker-b") == "delta-1"
    finally:
        state.close()


@pytest.mark.asyncio
async def test_knowledge_stream_restarts_from_durable_acknowledgement(tmp_path: Path) -> None:
    state = _initialized(tmp_path)
    try:
        state.append_event(
            AppendEventCommand(
                "KnowledgeDeltaPublished",
                "promotion",
                "run-1",
                None,
                _delta("delta-1", "a" * 64, 0, "b" * 64, 1),
            )
        )
        stream = KnowledgeStream(state)
        assert [item.delta_id for item in await stream.read("run-1", "worker-b", 0)] == ["delta-1"]
    finally:
        state.close()

    restarted = StateService(StateServiceConfig(tmp_path, "knowledge-restarted"))
    try:
        stream = KnowledgeStream(restarted)

        assert stream.last_acknowledged("run-1", "worker-b") == "delta-1"
        assert await stream.read("run-1", "worker-b", 1) == ()
    finally:
        restarted.close()
