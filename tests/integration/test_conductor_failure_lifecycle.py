from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import aizim.orchestration.conductor as conductor_module
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


class ImmediateFailureBackend:
    async def run(self, request: AgentRequest) -> AgentResult:
        del request
        raise RuntimeError("BACKEND_FAILED")


class CleanupCrashingConsumer:
    def __init__(self, *arguments: object) -> None:
        del arguments

    def notify_submission(self) -> None:
        return None

    async def run(self, stop: asyncio.Event) -> None:
        await stop.wait()
        raise RuntimeError("CONSUMER_CLEANUP_FAILED")

    async def wait_for_failure(self) -> None:
        await asyncio.Event().wait()


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


async def test_consumer_failure_cannot_skip_broker_and_alias_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = StateService(StateServiceConfig(tmp_path, "consumer-cleanup"))
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
    alias_paths: list[Path] = []
    original_alias = conductor_module.ProjectSocketAlias

    class CapturingAlias(original_alias):
        def __init__(self, project_root: Path) -> None:
            super().__init__(project_root)
            alias_paths.append(self.socket_path)

    monkeypatch.setattr(conductor_module, "ProjectSocketAlias", CapturingAlias)
    monkeypatch.setattr(conductor_module, "PromotionConsumer", CleanupCrashingConsumer)
    conductor = ResearchConductor(
        state,
        tmp_path,
        SMOKE_ROOT,
        ResourceGovernor(ResourcePolicy(), disk_free=lambda _path: 3_000_000_000),
    )

    def factory(_worker_id: str, _round: int, _delta: dict[str, str] | None):
        return ImmediateFailureBackend()

    try:
        with pytest.raises(RuntimeError, match="BACKEND_FAILED"):
            await asyncio.wait_for(conductor.run_two_worker(factory), timeout=1)

        assert alias_paths
        assert not (tmp_path / ".aizim/run/gateway.sock").exists()
        assert not alias_paths[0].parents[3].exists()
        assert state.active_document_leases() == ()
    finally:
        state.close()
