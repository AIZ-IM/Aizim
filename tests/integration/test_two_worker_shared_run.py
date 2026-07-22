from __future__ import annotations

import os
from pathlib import Path

import pytest

from aizim.agents import AgentRequest, AgentResult, FakeAgentBackend
from aizim.config.model import ResourcePolicy
from aizim.domain import EpochPair
from aizim.gateway import connect_gateway
from aizim.knowledge import KnowledgeReader
from aizim.lean.project import smoke_base_epoch
from aizim.orchestration.conductor import ResearchConductor
from aizim.orchestration.resources import ResourceGovernor
from aizim.state import AppendEventCommand, StateService, StateServiceConfig

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"
FIXTURES = Path(__file__).parents[1] / "fixtures" / "fake_workers"


class SocketCapturingBackend:
    def __init__(
        self,
        backend: FakeAgentBackend,
        sockets: list[Path],
        canonical_roots: list[Path],
        canonical_bound: list[bool],
        timeouts: list[float],
    ) -> None:
        self._backend, self._sockets = backend, sockets
        self._canonical_roots, self._canonical_bound = canonical_roots, canonical_bound
        self._timeouts = timeouts

    async def run(self, request: AgentRequest) -> AgentResult:
        self._sockets.append(request.gateway_broker_socket)
        self._timeouts.append(request.timeout_seconds)
        canonical = request.gateway_broker_socket.parents[2].resolve() / ".aizim/run/gateway.sock"
        self._canonical_roots.append(canonical.parents[2])
        self._canonical_bound.append(canonical.is_socket())
        return await self._backend.run(request)


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
        grants: list[tuple[str, ...]] = []
        for record in events:
            if record.envelope.event_type != "CapabilityMinted":
                continue
            operations = record.envelope.payload["operations"]
            assert type(operations) is tuple
            parsed: list[str] = []
            for operation in operations:
                assert type(operation) is str
                parsed.append(operation)
            grants.append(tuple(parsed))
        assert grants == [
            (
                "lean.goal",
                "lean.multi_attempt",
                "document.apply",
                "contribution.submit",
                "knowledge.read",
            ),
            ("lean.goal", "knowledge.read"),
            ("document.apply", "contribution.submit"),
        ]
        assert types.count("AgentRunCompleted") == 3
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


@pytest.mark.lean_integration
@pytest.mark.asyncio
async def test_worker_factory_uses_a_short_alias_to_the_canonical_gateway_socket(
    tmp_path: Path,
) -> None:
    epoch = EpochPair(smoke_base_epoch(SMOKE_ROOT), 0)
    state = StateService(StateServiceConfig(tmp_path, "factory-socket"))
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
    sockets: list[Path] = []
    canonical_roots: list[Path] = []
    canonical_bound: list[bool] = []
    timeouts: list[float] = []

    def factory(worker_id: str, round_index: int, delta: dict[str, str] | None):
        fixture = FIXTURES / ("prover_a.json" if worker_id == "prover-a" else "prover_b.json")
        return SocketCapturingBackend(
            FakeAgentBackend.from_fixture(
                fixture,
                connect=connect_gateway,
                cursor={},
                round_index=round_index,
                known_delta=delta,
            ),
            sockets,
            canonical_roots,
            canonical_bound,
            timeouts,
        )

    try:
        result = await conductor.run_two_worker(factory)

        socket = tmp_path / ".aizim" / "run" / "gateway.sock"
        assert result.verified_declarations == 2
        assert len(sockets) == 3
        assert all(item.name == "gateway.sock" for item in sockets)
        assert canonical_roots == [tmp_path.resolve()] * 3
        assert all(len(os.fsencode(item)) <= 103 for item in sockets)
        assert canonical_bound == [True, True, True]
        assert timeouts == [60.0, 60.0, 60.0]
        assert not socket.exists()
    finally:
        state.close()
