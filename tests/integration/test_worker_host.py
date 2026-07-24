from __future__ import annotations

import asyncio  # noqa: ANYIO_OK - existing host lifecycle
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Never

import pytest

import aizim.orchestration.worker_host as worker_host_module
from aizim.agents import AgentRequest, AgentResult
from aizim.config.model import ResourcePolicy
from aizim.domain import AgentRole, EpochPair, sha256_bytes
from aizim.domain.serialization import JsonValue
from aizim.gateway import (
    CapabilityGrant,
    CapabilityIssuer,
    GatewaySessionBroker,
    GatewayTool,
    advertised_tools,
)
from aizim.gateway.socket_alias import ProjectSocketAlias
from aizim.knowledge import ArtifactStore, RuntimePromotionVerifier
from aizim.lean import BrokerDependencies, DocumentBroker, SharedLeanRuntime
from aizim.lean.project import smoke_base_epoch
from aizim.orchestration.knowledge_stream import KnowledgeStream
from aizim.orchestration.resources import ResourceGovernor
from aizim.orchestration.worker_authority import WorkerDirective
from aizim.orchestration.worker_gateway import WorkerGatewayActions
from aizim.orchestration.worker_host import WorkerExecutionHost, _HostFactories
from aizim.state import AppendEventCommand, StateService, StateServiceConfig

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"
NOW = datetime(2030, 1, 1, tzinfo=UTC)
_SESSION_CLOSE = GatewaySessionBroker.aclose
_RUNTIME_CLOSE = SharedLeanRuntime.aclose


class WorkerHostTestError(RuntimeError):
    pass


class RecordingSubmittedBackend:
    def __init__(self, requests: list[AgentRequest]) -> None:
        self._requests = requests

    async def run(self, request: AgentRequest) -> AgentResult:
        self._requests.append(request)
        return AgentResult(request.worker_id, "submitted", "done", "a" * 64, "b" * 64, 0)


class CleanupCrashingConsumer:
    def __init__(
        self,
        state: StateService,
        artifacts: ArtifactStore,
        verifier: RuntimePromotionVerifier,
        knowledge: KnowledgeStream,
    ) -> None:
        del state, artifacts, verifier, knowledge

    def notify_submission(self) -> None:
        raise WorkerHostTestError("NOTIFY_CLEANUP_FAILED")

    async def run(self, stop: asyncio.Event) -> None:
        await stop.wait()

    async def wait_for_failure(self) -> None:
        await asyncio.Event().wait()

    async def wait_for_epoch(self, epoch: int) -> None:
        del epoch
        await asyncio.Event().wait()


class CleanupTracker:
    def __init__(self) -> None:
        self.sessions_closed = 0
        self.runtimes_closed = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def close_session(session: GatewaySessionBroker) -> None:
            self.sessions_closed += 1
            await _SESSION_CLOSE(session)

        async def close_runtime(runtime: SharedLeanRuntime) -> None:
            self.runtimes_closed += 1
            await _RUNTIME_CLOSE(runtime)

        monkeypatch.setattr(GatewaySessionBroker, "aclose", close_session)
        monkeypatch.setattr(SharedLeanRuntime, "aclose", close_runtime)


def _state(tmp_path: Path, session: str) -> StateService:
    state = StateService(StateServiceConfig(tmp_path, session))
    payload: dict[str, JsonValue] = {
        "project_id": "project",
        "base_epoch": smoke_base_epoch(SMOKE_ROOT),
        "knowledge_epoch": 0,
    }
    state.append_event(AppendEventCommand("ProjectInitialized", "supervisor", None, None, payload))
    return state


def _governor() -> ResourceGovernor:
    return ResourceGovernor(ResourcePolicy(), disk_free=lambda _path: 3_000_000_000)


def _directive(worker_id: str) -> WorkerDirective:
    source = b"import Std\n\ntheorem candidate : True := by\n  sorry\n"
    role, tools = AgentRole.PROOF_EXPLORER, (GatewayTool.KNOWLEDGE_READ,)
    return WorkerDirective(f"directive-{worker_id}", worker_id, role, source, 3, 1.0, tools)


async def test_host_runs_one_worker_and_closes_every_owned_resource(tmp_path: Path) -> None:
    state = _state(tmp_path, "host-lifecycle")
    host = await WorkerExecutionHost.open(state, tmp_path, SMOKE_ROOT, _governor(), "run-1")
    requests: list[AgentRequest] = []
    try:
        cursor = await host.run(_directive("worker-1"), RecordingSubmittedBackend(requests))
        assert cursor.terminal
    finally:
        await host.aclose()

    try:
        events = [record.envelope.event_type for record in state.query_events("run-1")]
        required = "ScheduleProposed WorkerStarted AgentRunCompleted WorkerStopped LeaseReleased"
        assert requests and requests[0].gateway_broker_socket.name == "gateway.sock"
        assert set(required.split()).issubset(events)
        assert state.active_document_leases() == ()
        assert not (tmp_path / ".aizim/run/gateway.sock").exists()
    finally:
        state.close()


async def test_partial_open_failure_closes_alias_session_and_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path, "partial-open")
    aliases: list[ProjectSocketAlias] = []
    tracker = CleanupTracker()

    class CapturingAlias(ProjectSocketAlias):
        def __init__(self, project_root: Path) -> None:
            super().__init__(project_root)
            aliases.append(self)

    def fail_authority(
        issuer: CapabilityIssuer, sessions: GatewaySessionBroker, client_image_hash: str
    ) -> Never:
        del issuer, sessions, client_image_hash
        raise WorkerHostTestError("AUTHORITY_CONSTRUCTION_FAILED")

    monkeypatch.setattr(worker_host_module, "ProjectSocketAlias", CapturingAlias)
    monkeypatch.setattr(worker_host_module, "BrokerWorkerAuthority", fail_authority)
    tracker.install(monkeypatch)
    try:
        with pytest.raises(WorkerHostTestError, match="AUTHORITY_CONSTRUCTION_FAILED"):
            await WorkerExecutionHost.open(state, tmp_path, SMOKE_ROOT, _governor(), "run-partial")
        assert (tracker.sessions_closed, tracker.runtimes_closed) == (1, 1)
        assert aliases and not aliases[0].socket_path.parents[3].exists()
        assert not (tmp_path / ".aizim/run/gateway.sock").exists()
    finally:
        if aliases and aliases[0].socket_path.parents[3].exists():
            aliases[0].close()
        state.close()


@pytest.mark.parametrize("primary", (True, False), ids=("primary", "no-primary"))
async def test_notify_failure_is_observed_after_closing_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, primary: bool
) -> None:
    state = _state(tmp_path, f"notify-{primary}")
    tracker = CleanupTracker()
    tracker.install(monkeypatch)
    host = await WorkerExecutionHost._open_with_factories(
        state,
        tmp_path,
        SMOKE_ROOT,
        _governor(),
        f"run-notify-{primary}",
        _HostFactories(ProjectSocketAlias, CleanupCrashingConsumer),
    )
    alias_directory = host._resources.alias.socket_path.parents[3]
    try:
        if primary:
            with pytest.raises(WorkerHostTestError, match="PRIMARY_FAILED") as raised:
                try:
                    raise WorkerHostTestError("PRIMARY_FAILED")
                finally:
                    await host.aclose()
            assert any("NOTIFY_CLEANUP_FAILED" in note for note in raised.value.__notes__)
        else:
            with pytest.raises(WorkerHostTestError, match="NOTIFY_CLEANUP_FAILED"):
                await host.aclose()
        assert (tracker.sessions_closed, tracker.runtimes_closed) == (1, 1)
        assert not alias_directory.exists()
        assert not (tmp_path / ".aizim/run/gateway.sock").exists()
    finally:
        if alias_directory.exists():
            await host._resources.sessions.aclose()
            await host._resources.runtime.aclose()
            host._resources.alias.close()
        state.close()


async def test_document_recovery_restores_prepared_state_and_revokes_capability(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path, "before-restart")
    epoch = EpochPair(smoke_base_epoch(SMOKE_ROOT), 0)
    dependencies = BrokerDependencies(lambda: NOW, lambda: "lease-1", lambda: "document-1")
    broker = DocumentBroker(tmp_path, state, smoke_root=SMOKE_ROOT, dependencies=dependencies)
    path = PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean")
    lease = await broker.create_document("run-1", "worker-1", path, b"committed\n", epoch)
    role, tools = AgentRole.PROOF_EXPLORER, (GatewayTool.KNOWLEDGE_READ,)
    grant = CapabilityGrant("run-1", "worker-1", role, lease.lease_id, tools, lease.expires_at)
    raw_token = CapabilityIssuer(state, token_factory=lambda: "token-1").mint(grant)
    lease_id, document_id = lease.lease_id, lease.document_id
    content_hash = sha256_bytes(b"committed\n")
    state.prepare_document_edit("run-1", "worker-1", lease_id, document_id, 0, content_hash)
    physical = tmp_path / ".aizim/run/run-1/lean-project" / path
    physical.write_bytes(b"crash-window\n")
    state.close()

    restarted = StateService(StateServiceConfig(tmp_path, "after-restart"))
    recovered = DocumentBroker(
        tmp_path, restarted, smoke_root=SMOKE_ROOT, dependencies=dependencies
    )
    await recovered.recover()
    first_events = tuple(restarted.query_events("run-1"))
    await recovered.recover()
    try:
        capability = restarted.capability_record(sha256(raw_token.encode()).hexdigest())
        assert physical.read_bytes() == b"committed\n"
        assert restarted.active_document_leases() == ()
        assert capability is not None and capability.revoked_at is not None
        event_types = tuple(item.envelope.event_type for item in first_events)
        assert event_types[-2:] == ("DocumentEditRecovered", "LeaseRecovered")
        assert restarted.query_events("run-1") == first_events
    finally:
        restarted.close()


def test_controller_worker_tools_are_ordered_implemented_role_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aizim.orchestration.worker_gateway import (
        CONTROLLER_WORKER_TOOLS,
        controller_worker_tools,
    )

    state = _state(tmp_path, "controller-tools")
    broker = DocumentBroker(tmp_path, state, smoke_root=SMOKE_ROOT)
    runtime = SharedLeanRuntime(state, broker, "run-1")
    knowledge, artifacts = KnowledgeStream(state), ArtifactStore(tmp_path)
    actions = WorkerGatewayActions(
        state, broker, runtime, knowledge, artifacts, "e" * 64, lambda: None
    )
    try:
        actual = set(actions.targets())
        allowed = set(CONTROLLER_WORKER_TOOLS) & actual
        for role in AgentRole:
            expected = tuple(tool for tool in advertised_tools(role) if tool in allowed)
            assert controller_worker_tools(role) == expected
        assert GatewayTool.PROJECT_READ not in controller_worker_tools(AgentRole.PROOF_EXPLORER)
        registry = (GatewayTool.KNOWLEDGE_READ,)
        monkeypatch.setattr(WorkerGatewayActions, "_TARGET_TOOLS", registry, raising=False)
        assert tuple(actions.targets()) == registry
        assert controller_worker_tools(AgentRole.PROOF_EXPLORER) == registry
    finally:
        state.close()
