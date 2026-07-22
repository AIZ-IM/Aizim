from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aizim.agents import AgentRequest, AgentResult
from aizim.config.model import ResourcePolicy
from aizim.lean.project import smoke_base_epoch
from aizim.orchestration.conductor import ResearchConductor
from aizim.orchestration.resources import ResourceGovernor
from aizim.state import AppendEventCommand, StateService, StateServiceConfig

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"


class ControlledBackend:
    def __init__(
        self,
        worker_id: str,
        waiting_started: asyncio.Event,
        release: asyncio.Event,
        cancelled: asyncio.Event,
    ) -> None:
        self._worker_id = worker_id
        self._waiting_started = waiting_started
        self._release = release
        self._cancelled = cancelled

    async def run(self, request: AgentRequest) -> AgentResult:
        if self._worker_id == "prover-a":
            await self._waiting_started.wait()
            raise RuntimeError("BACKEND_FAILED")
        self._waiting_started.set()
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self._cancelled.set()
            raise
        return AgentResult(request.worker_id, "submitted", "released", "a" * 64, "b" * 64, 0)


async def test_worker_failure_is_joined_before_conductor_returns(tmp_path: Path) -> None:
    state = StateService(StateServiceConfig(tmp_path, "worker-failure"))
    state.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {
                "project_id": "project",
                "base_epoch": smoke_base_epoch(SMOKE_ROOT),
                "knowledge_epoch": 0,
            },
        )
    )
    waiting_started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    conductor = ResearchConductor(
        state,
        tmp_path,
        SMOKE_ROOT,
        ResourceGovernor(ResourcePolicy(), disk_free=lambda _path: 3_000_000_000),
    )

    def factory(worker_id: str, _round: int, _delta: dict[str, str] | None):
        return ControlledBackend(worker_id, waiting_started, release, cancelled)

    try:
        with pytest.raises(RuntimeError, match="BACKEND_FAILED"):
            await asyncio.wait_for(conductor.run_two_worker(factory), timeout=1)
        assert cancelled.is_set()
        assert state.active_document_leases() == ()
        assert (
            sum(record.envelope.event_type == "LeaseReleased" for record in state.query_events())
            == 2
        )
    finally:
        release.set()
        for _attempt in range(100):
            terminal = any(
                record.envelope.event_type in {"WorkerCrashed", "WorkerStopped"}
                and record.envelope.payload.get("worker_id") == "prover-b"
                for record in state.query_events()
            )
            if terminal:
                break
            await asyncio.sleep(0.01)
        state.close()
