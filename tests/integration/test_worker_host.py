from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath

import pytest

from aizim.agents import AgentRequest, AgentResult
from aizim.config.model import ResourcePolicy
from aizim.domain import AgentRole, EpochPair, sha256_bytes
from aizim.gateway import (
    CapabilityGrant,
    CapabilityIssuer,
    GatewayTool,
    advertised_tools,
)
from aizim.knowledge import ArtifactStore
from aizim.lean import BrokerDependencies, DocumentBroker, SharedLeanRuntime
from aizim.lean.project import smoke_base_epoch
from aizim.orchestration.knowledge_stream import KnowledgeStream
from aizim.orchestration.resources import ResourceGovernor
from aizim.orchestration.worker_authority import WorkerDirective
from aizim.orchestration.worker_gateway import WorkerGatewayActions
from aizim.orchestration.worker_host import WorkerExecutionHost
from aizim.state import AppendEventCommand, StateService, StateServiceConfig

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"
NOW = datetime(2030, 1, 1, tzinfo=UTC)


class RecordingSubmittedBackend:
    def __init__(self, requests: list[AgentRequest]) -> None:
        self._requests = requests

    async def run(self, request: AgentRequest) -> AgentResult:
        self._requests.append(request)
        return AgentResult(request.worker_id, "submitted", "done", "a" * 64, "b" * 64, 0)


def _state(tmp_path: Path, session: str) -> StateService:
    state = StateService(StateServiceConfig(tmp_path, session))
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
    return state


def _governor() -> ResourceGovernor:
    return ResourceGovernor(ResourcePolicy(), disk_free=lambda _path: 3_000_000_000)


def _directive(worker_id: str) -> WorkerDirective:
    return WorkerDirective(
        f"directive-{worker_id}",
        worker_id,
        AgentRole.PROOF_EXPLORER,
        b"import Std\n\ntheorem candidate : True := by\n  sorry\n",
        3,
        1.0,
        (GatewayTool.KNOWLEDGE_READ,),
    )


@pytest.mark.asyncio
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
        assert requests and requests[0].gateway_broker_socket.name == "gateway.sock"
        assert all(
            event in events
            for event in (
                "ScheduleProposed",
                "WorkerStarted",
                "AgentRunCompleted",
                "WorkerStopped",
                "LeaseReleased",
            )
        )
        assert state.active_document_leases() == ()
        assert not (tmp_path / ".aizim/run/gateway.sock").exists()
    finally:
        state.close()


@pytest.mark.asyncio
async def test_document_recovery_restores_prepared_state_and_revokes_capability(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path, "before-restart")
    epoch = EpochPair(smoke_base_epoch(SMOKE_ROOT), 0)
    dependencies = BrokerDependencies(
        clock=lambda: NOW,
        lease_ids=lambda: "lease-1",
        document_ids=lambda: "document-1",
    )
    broker = DocumentBroker(tmp_path, state, smoke_root=SMOKE_ROOT, dependencies=dependencies)
    path = PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean")
    lease = await broker.create_document("run-1", "worker-1", path, b"committed\n", epoch)
    raw_token = CapabilityIssuer(state, token_factory=lambda: "token-1").mint(
        CapabilityGrant(
            "run-1",
            "worker-1",
            AgentRole.PROOF_EXPLORER,
            lease.lease_id,
            (GatewayTool.KNOWLEDGE_READ,),
            lease.expires_at,
        )
    )
    state.prepare_document_edit(
        "run-1",
        "worker-1",
        lease.lease_id,
        lease.document_id,
        0,
        sha256_bytes(b"committed\n"),
    )
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
        assert [item.envelope.event_type for item in first_events][-2:] == [
            "DocumentEditRecovered",
            "LeaseRecovered",
        ]
        assert restarted.query_events("run-1") == first_events
    finally:
        restarted.close()


def test_controller_worker_tools_are_ordered_implemented_role_tools(
    tmp_path: Path,
) -> None:
    from aizim.orchestration.worker_gateway import (
        CONTROLLER_WORKER_TOOLS,
        controller_worker_tools,
    )

    state = _state(tmp_path, "controller-tools")
    broker = DocumentBroker(tmp_path, state, smoke_root=SMOKE_ROOT)
    runtime = SharedLeanRuntime(state, broker, "run-1")
    actions = WorkerGatewayActions(
        state,
        broker,
        runtime,
        KnowledgeStream(state),
        ArtifactStore(tmp_path),
        "e" * 64,
        lambda: None,
    )
    try:
        actual = set(actions.targets())
        for role in AgentRole:
            expected = tuple(
                tool
                for tool in advertised_tools(role)
                if tool in CONTROLLER_WORKER_TOOLS and tool in actual
            )
            assert controller_worker_tools(role) == expected
        assert GatewayTool.PROJECT_READ not in controller_worker_tools(AgentRole.PROOF_EXPLORER)
    finally:
        state.close()
