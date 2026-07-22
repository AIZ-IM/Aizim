from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aizim.agents import AgentRequest, AgentResult
from aizim.config.model import ResourcePolicy
from aizim.domain import AgentRole, EpochPair, FileLease
from aizim.lean import DocumentBroker
from aizim.lean.project import smoke_base_epoch
from aizim.orchestration.resources import ResourceGovernor
from aizim.orchestration.worker import GatewaySession, WorkerDirective, WorkerRunner
from aizim.state import AppendEventCommand, StateService, StateServiceConfig

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"


class StaticAuthority:
    def __init__(self, root: Path) -> None:
        self._root = root

    def issue(self, run_id: str, directive: WorkerDirective, lease: FileLease) -> GatewaySession:
        del run_id, directive, lease
        return GatewaySession(self._root / "gateway.sock", "session")


class CrashBackend:
    async def run(self, request: AgentRequest) -> AgentResult:
        del request
        raise RuntimeError("injected worker crash")


class CompletedBackend:
    async def run(self, request: AgentRequest) -> AgentResult:
        return AgentResult(request.worker_id, "submitted", "done", "a" * 64, "b" * 64, 0)


class WaitingBackend:
    async def run(self, request: AgentRequest) -> AgentResult:
        del request
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _open(tmp_path: Path, session: str) -> tuple[StateService, DocumentBroker]:
    state = StateService(StateServiceConfig(tmp_path, session))
    epoch = EpochPair(smoke_base_epoch(SMOKE_ROOT), 0)
    state.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {"project_id": "project", "base_epoch": epoch.base_epoch, "knowledge_epoch": 0},
        )
    )
    return state, DocumentBroker(tmp_path, state, smoke_root=SMOKE_ROOT)


def _directive(timeout: float = 1.0) -> WorkerDirective:
    return WorkerDirective(
        "directive-a",
        "worker-a",
        AgentRole.PROOF_EXPLORER,
        b"import Std\n\ntheorem candidate : True := by\n  sorry\n",
        3,
        timeout,
    )


def _runner(state: StateService, broker: DocumentBroker, root: Path) -> WorkerRunner:
    return WorkerRunner(
        state,
        broker,
        ResourceGovernor(ResourcePolicy(), disk_free=lambda _path: 3_000_000_000),
        StaticAuthority(root),
        root,
        "run-1",
    )


@pytest.mark.asyncio
async def test_crashed_worker_resumes_with_new_execution_and_recovers_lease(tmp_path: Path) -> None:
    state, broker = _open(tmp_path, "first")
    runner = _runner(state, broker, tmp_path)
    try:
        with pytest.raises(RuntimeError, match="injected worker crash"):
            await runner.run(_directive(), CrashBackend())
        crashed = runner.cursor("worker-a")
        assert crashed is not None and crashed.terminal
        assert crashed.lease_id is not None
        assert not runner.heartbeat("worker-a")
        assert "WorkerCrashed" in [
            item.envelope.event_type for item in state.query_events("run-1")
        ]
    finally:
        state.close()

    restarted = StateService(StateServiceConfig(tmp_path, "second"))
    resumed_broker = DocumentBroker(tmp_path, restarted, smoke_root=SMOKE_ROOT)
    resumed = _runner(restarted, resumed_broker, tmp_path)
    try:
        previous = resumed.cursor("worker-a")
        result = await resumed.run(_directive(), CompletedBackend())

        assert previous is not None
        assert result.execution_id != previous.execution_id
        assert result.terminal
        events = [item.envelope.event_type for item in restarted.query_events("run-1")]
        assert "LeaseRecovered" in events
        assert events[-1] == "WorkerCursorSaved"
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_worker_timeout_records_terminal_event(tmp_path: Path) -> None:
    state, broker = _open(tmp_path, "timeout")
    try:
        result = await _runner(state, broker, tmp_path).run(_directive(0.01), WaitingBackend())

        assert result.terminal
        event_types = [item.envelope.event_type for item in state.query_events("run-1")]
        assert "WorkerTimedOut" in event_types
    finally:
        state.close()
