from __future__ import annotations

from pathlib import Path

import pytest

from aizim.config.model import ResourcePolicy
from aizim.domain import EpochPair
from aizim.knowledge import KnowledgeReader
from aizim.lean.project import smoke_base_epoch
from aizim.orchestration.conductor import ResearchConductor
from aizim.orchestration.resources import ResourceGovernor
from aizim.state import AppendEventCommand, StateService, StateServiceConfig

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"
FIXTURES = Path(__file__).parents[1] / "fixtures" / "fake_workers"


@pytest.mark.lean_integration
@pytest.mark.asyncio
async def test_two_workers_share_one_runtime_and_publish_ordered_knowledge(tmp_path: Path) -> None:
    epoch = EpochPair(smoke_base_epoch(SMOKE_ROOT), 0)
    state = StateService(StateServiceConfig(tmp_path, "two-worker"))
    state.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {"project_id": "project", "base_epoch": epoch.base_epoch, "knowledge_epoch": 0},
        )
    )
    conductor = ResearchConductor(
        state,
        tmp_path,
        SMOKE_ROOT,
        ResourceGovernor(ResourcePolicy(), disk_free=lambda _path: 3_000_000_000),
    )
    try:
        result = await conductor.run_fake(FIXTURES / "prover_a.json", FIXTURES / "prover_b.json")

        assert result.start_knowledge_epoch == 0
        assert result.end_knowledge_epoch == 2
        assert result.verified_declarations == 2
        events = state.query_events(result.run_id)
        types = [record.envelope.event_type for record in events]
        assert types.count("LeanRuntimeStarted") == types.count("LeanRuntimeStopped") == 1
        started = [
            record.sequence for record in events if record.envelope.event_type == "WorkerStarted"
        ]
        stopped = [
            record.sequence for record in events if record.envelope.event_type == "WorkerStopped"
        ]
        assert len(started) >= 2 and max(started[:2]) < min(stopped)
        a_delta = next(
            record.sequence
            for record in events
            if record.envelope.event_type == "KnowledgeDeltaPublished"
            and record.envelope.payload["knowledge_epoch"] == 1
        )
        b_ack = next(
            record.sequence
            for record in events
            if record.envelope.event_type == "KnowledgeDeltaAcknowledged"
            and record.envelope.payload["worker_id"] == "prover-b"
        )
        b_edit = next(
            record.sequence
            for record in events
            if record.envelope.event_type == "DocumentEdited"
            and record.envelope.payload["worker_id"] == "prover-b"
        )
        assert a_delta < b_ack < b_edit
        deltas = KnowledgeReader(state).read(0)
        pairs = [
            (item.previous_epoch.knowledge_epoch, item.new_epoch.knowledge_epoch) for item in deltas
        ]
        assert pairs == [
            (0, 1),
            (1, 2),
        ]
    finally:
        state.close()
