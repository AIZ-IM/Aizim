from __future__ import annotations

import asyncio  # ANYIO_OK: controlled worker lifecycle clocks
from collections.abc import AsyncIterator
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from integration.gateway_support import Ids, MutableClock, service

from aizim.agents import AgentRequest, AgentResult
from aizim.config.model import ResourcePolicy
from aizim.domain import AgentRole, EpochPair, FileLease
from aizim.gateway import (
    CapabilityDependencies,
    CapabilityGateway,
    CapabilityIssuer,
    CapabilitySession,
    GatewayFailure,
    GatewayLimits,
    GatewaySessionBroker,
    GatewaySuccess,
    GatewayTool,
)
from aizim.lean import DocumentBroker
from aizim.lean.models import BrokerDependencies
from aizim.lean.project import smoke_base_epoch
from aizim.orchestration.resources import ResourceGovernor
from aizim.orchestration.worker import WorkerExecutionError, WorkerRunner
from aizim.orchestration.worker_authority import (
    BrokerWorkerAuthority,
    GatewaySession,
    WorkerDirective,
)
from aizim.state import AppendEventCommand

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"
TOKEN = "worker-lifetime-token"
TOKEN_HASH = sha256(TOKEN.encode()).hexdigest()
SOURCE = b"import Std\n\ntheorem candidate : True := by\n  sorry\n"


class ExecutionClock(MutableClock):
    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.monotonic += seconds


class WorkerHarness:
    def __init__(self, root: Path, clock: ExecutionClock) -> None:
        self.clock = clock
        self.state = service(root, clock)
        epoch = EpochPair(smoke_base_epoch(SMOKE_ROOT), 0)
        self.state.append_event(
            AppendEventCommand(
                "ProjectInitialized",
                "supervisor",
                None,
                None,
                {"project_id": "project", "base_epoch": epoch.base_epoch, "knowledge_epoch": 0},
            )
        )
        self.broker = DocumentBroker(
            root, self.state, smoke_root=SMOKE_ROOT,
            dependencies=BrokerDependencies(clock=clock.utc_now),
        )
        self.governor = ResourceGovernor(
            ResourcePolicy(max_proof_workers=1, scratch_slots=1),
            disk_free=lambda _path: 3_000_000_000,
        )
        self.gateway = CapabilityGateway(
            self.state,
            {GatewayTool.KNOWLEDGE_READ: lambda _call: {"knowledge_epoch": 0}},
            GatewayLimits(100, 60.0, 100),
            CapabilityDependencies(clock.utc_now, clock.monotonic_now, Ids("request")),
        )
        self.sessions = GatewaySessionBroker(root / "gateway.sock", gateway=self.gateway)
        self.authority = BrokerWorkerAuthority(
            CapabilityIssuer(self.state, token_factory=lambda: TOKEN), self.sessions, "a" * 64,
        )
        self.runner = WorkerRunner(
            self.state, self.broker, self.governor, self.authority, root, "run-1",
        )

    def session(self, lease_id: str) -> CapabilitySession:
        return CapabilitySession(
            TOKEN, "run-1", "worker-1", AgentRole.PROOF_EXPLORER.value, lease_id,
        )

    def assert_released(self) -> None:
        assert self.state.active_document_leases() == ()
        capability = self.state.capability_record(TOKEN_HASH)
        assert capability is not None and capability.revoked_at is not None


@pytest.fixture
async def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkerHarness]:
    clock = ExecutionClock()
    loop = asyncio.get_running_loop()
    clock.monotonic = loop.time()
    monkeypatch.setattr(loop, "time", clock.monotonic_now)
    worker = WorkerHarness(tmp_path, clock)
    try:
        yield worker
    finally:
        await worker.sessions.aclose()
        worker.state.close()


def _directive(timeout: float) -> WorkerDirective:
    return WorkerDirective(
        "directive-1", "worker-1", AgentRole.PROOF_EXPLORER, SOURCE, 10, timeout,
        (GatewayTool.KNOWLEDGE_READ,),
    )


@pytest.mark.parametrize("timeout", [900.0, 14400.0])
async def test_queued_worker_authority_covers_setup_and_execution_deadline(
    harness: WorkerHarness, monkeypatch: pytest.MonkeyPatch, timeout: float,
) -> None:
    clock, state = harness.clock, harness.state
    issue = harness.authority.issue
    setup_seconds = 11.0

    def delayed_issue(
        run_id: str, directive: WorkerDirective, lease: FileLease,
    ) -> GatewaySession:
        clock.advance(setup_seconds)
        return issue(run_id, directive, lease)

    monkeypatch.setattr(harness.authority, "issue", delayed_issue)

    class CallingBackend:
        async def run(self, request: AgentRequest) -> AgentResult:
            capability = state.capability_record(TOKEN_HASH)
            assert capability is not None and capability.lease_id is not None
            assert capability.expires_at == admitted_at + timedelta(seconds=timeout)
            # Use nearly the whole shared deadline, including time spent preparing authority.
            clock.advance(timeout - setup_seconds - 0.25)
            result = await harness.gateway.call(
                harness.session(capability.lease_id), GatewayTool.KNOWLEDGE_READ, {},
            )
            assert isinstance(result, GatewaySuccess)
            snapshot = await harness.broker.read_document(
                request.run_id, request.worker_id, capability.lease_id,
                str(request.context["document_id"]),
            )
            assert snapshot.content == SOURCE
            return AgentResult(request.worker_id, "submitted", "done", "a" * 64, "b" * 64, 0)

    async with harness.governor.proof_slot():
        task = asyncio.create_task(harness.runner.run(_directive(timeout), CallingBackend()))
        await asyncio.sleep(0)
        assert state.active_document_leases() == ()
        assert state.capability_record(TOKEN_HASH) is None
        clock.advance(1800.0)
        admitted_at = clock.wall
    try:
        cursor = await task
        assert cursor.terminal
        harness.assert_released()
        assert cursor.lease_id is not None
        denied = await harness.gateway.call(
            harness.session(cursor.lease_id), GatewayTool.KNOWLEDGE_READ, {},
        )
        assert isinstance(denied, GatewayFailure)
        assert state.query_events("run-1")[-1].envelope.payload["reason_code"] == "TOKEN_REVOKED"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_setup_exhausting_deadline_never_starts_backend(
    harness: WorkerHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = harness.runner._request
    started = False

    def delayed_request(
        directive: WorkerDirective, lease: FileLease, session: GatewaySession,
    ) -> AgentRequest:
        result = request(directive, lease, session)
        harness.clock.advance(directive.timeout_seconds)
        return result

    monkeypatch.setattr(harness.runner, "_request", delayed_request)

    class UnexpectedBackend:
        async def run(self, request: AgentRequest) -> AgentResult:
            nonlocal started
            started = True
            raise AssertionError("backend started after the execution deadline")

    with pytest.raises(WorkerExecutionError, match="WORKER_TIMEOUT"):
        await harness.runner.run(_directive(900.0), UnexpectedBackend())
    assert not started
    harness.assert_released()
    assert "WorkerTimedOut" in [
        item.envelope.event_type for item in harness.state.query_events("run-1")
    ]
    async with harness.governor.proof_slot():
        pass


@pytest.mark.parametrize("stop", ["timeout", "cancelled", "crashed"])
async def test_worker_stop_releases_slots_lease_and_capability(
    harness: WorkerHarness, stop: str,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class StoppingBackend:
        async def run(self, request: AgentRequest) -> AgentResult:
            started.set()
            if stop == "crashed":
                raise RuntimeError("backend crashed")
            if stop == "timeout":
                harness.clock.advance(request.timeout_seconds)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            raise AssertionError("unreachable")

    task = asyncio.create_task(harness.runner.run(_directive(14400.0), StoppingBackend()))
    try:
        await started.wait()
        if stop == "cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif stop == "timeout":
            with pytest.raises(WorkerExecutionError, match="WORKER_TIMEOUT"):
                await task
        else:
            with pytest.raises(RuntimeError, match="backend crashed"):
                await task
        if stop != "crashed":
            assert cancelled.is_set()
        harness.assert_released()
        async with harness.governor.proof_slot():
            pass
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancelling_queued_worker_grants_no_authority(harness: WorkerHarness) -> None:
    class UnexpectedBackend:
        async def run(self, request: AgentRequest) -> AgentResult:
            raise AssertionError("queued worker started")

    async with harness.governor.proof_slot():
        task = asyncio.create_task(harness.runner.run(_directive(900.0), UnexpectedBackend()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert harness.state.active_document_leases() == ()
    assert harness.state.capability_record(TOKEN_HASH) is None
    events = [item.envelope.event_type for item in harness.state.query_events("run-1")]
    assert "WorkerCrashed" in events
    assert "LeaseGranted" not in events
    async with harness.governor.proof_slot():
        pass


async def test_document_setup_timeout_releases_reserved_slots(harness: WorkerHarness) -> None:
    class UnexpectedBackend:
        async def run(self, request: AgentRequest) -> AgentResult:
            raise AssertionError("document setup did not finish")

    await harness.broker._recovery_lock.acquire()
    task = asyncio.create_task(harness.runner.run(_directive(900.0), UnexpectedBackend()))
    try:
        await asyncio.sleep(0)
        harness.clock.advance(900.0)
        with pytest.raises(WorkerExecutionError, match="WORKER_TIMEOUT"):
            await task
        assert harness.state.active_document_leases() == ()
        assert harness.state.capability_record(TOKEN_HASH) is None
        assert "WorkerTimedOut" in [
            item.envelope.event_type for item in harness.state.query_events("run-1")
        ]
        async with harness.governor.proof_slot():
            pass
    finally:
        harness.broker._recovery_lock.release()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
