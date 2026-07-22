from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from aizim.domain import AgentRole, EpochPair
from aizim.gateway import (
    CapabilityDependencies,
    CapabilityGateway,
    CapabilityGrant,
    CapabilityIssuer,
    CapabilitySession,
    GatewayFailure,
    GatewayLimits,
    GatewaySuccess,
    GatewayTool,
)
from aizim.lean import BrokerDependencies, DocumentBroker
from aizim.lean.models import WorkerSession
from aizim.lean.project import smoke_base_epoch
from aizim.lean.runtime import SharedLeanRuntime
from aizim.state import AppendEventCommand, StateDependencies, StateService, StateServiceConfig
from aizim.state.events import event_as_dict

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"


def _source(namespace: str) -> bytes:
    return (
        "import Std\n\n"
        f"namespace {namespace}\n\n"
        "theorem goal (n : Nat) : n + 0 = n := by\n"
        "  sorry\n\n"
        f"end {namespace}\n"
    ).encode()


def _path(worker: str) -> PurePosixPath:
    return PurePosixPath(f"AizimSmoke/Workers/run-1/{worker}.lean")


@pytest.mark.lean_integration
@pytest.mark.asyncio
async def test_two_workers_share_one_runtime_and_gateway_hides_trusted_tools(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 22, 10, tzinfo=UTC)
    epoch = EpochPair(smoke_base_epoch(SMOKE_ROOT), 0)
    state = StateService(
        StateServiceConfig(tmp_path, "lean-runtime"),
        StateDependencies(clock=lambda: now),
    )
    state.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {"project_id": "project-1", "base_epoch": epoch.base_epoch, "knowledge_epoch": 0},
        )
    )
    broker = DocumentBroker(
        tmp_path,
        state,
        smoke_root=SMOKE_ROOT,
        dependencies=BrokerDependencies(clock=lambda: now, lease_lifetime=timedelta(minutes=5)),
    )
    runtime = SharedLeanRuntime(state, broker, "run-1")
    try:
        first = await broker.create_document(
            "run-1", "worker-1", _path("worker-1"), _source("One"), epoch
        )
        second = await broker.create_document(
            "run-1", "worker-2", _path("worker-2"), _source("Two"), epoch
        )
        first_session = WorkerSession("run-1", "worker-1", first.lease_id)
        second_session = WorkerSession("run-1", "worker-2", second.lease_id)

        first_goal, second_goal = await asyncio.gather(
            runtime.goal(first_session, first.document_id, 6),
            runtime.goal(second_session, second.document_id, 6),
        )

        assert (
            first_goal.goals_before[0].target == second_goal.goals_before[0].target == "n + 0 = n"
        )
        assert first_goal.goals_before[0].hypotheses[0].name == "n"
        diagnostics = await runtime.diagnostics(first_session, first.document_id)
        attempts = await runtime.multi_attempt(
            second_session,
            second.document_id,
            6,
            ("exact Nat.add_zero n",),
        )
        assert diagnostics.success
        assert attempts.items[0].diagnostics == ()
        processes = runtime.processes()
        mcp = [item for item in processes if "lean-lsp-mcp" in item.command]
        lake = [item for item in processes if "lake serve" in item.command]
        assert len(mcp) == len(lake) == 1
        assert lake[0].parent_pid == mcp[0].pid

        token = CapabilityIssuer(state).mint(
            CapabilityGrant(
                "run-1",
                "worker-1",
                AgentRole.PROOF_EXPLORER,
                first.lease_id,
                (GatewayTool.LEAN_GOAL,),
                now + timedelta(minutes=1),
            )
        )
        gateway = CapabilityGateway(
            state,
            runtime.gateway_targets(),
            GatewayLimits(20, 60.0, 20),
            CapabilityDependencies(
                clock=lambda: now, monotonic=lambda: 1.0, request_ids=lambda: "request-1"
            ),
        )
        worker = CapabilitySession(token, "run-1", "worker-1", "proof_explorer", first.lease_id)
        assert GatewayTool.LEAN_BUILD not in gateway.discover(worker)
        assert GatewayTool.LEAN_VERIFY not in gateway.discover(worker)
        denied = await gateway.call(worker, GatewayTool.LEAN_BUILD, {})
        assert isinstance(denied, GatewayFailure)
        called = await gateway.call(
            worker, GatewayTool.LEAN_GOAL, {"document_id": first.document_id, "line": 6}
        )
        assert isinstance(called, GatewaySuccess)
        assert isinstance(called.result, dict)
        goals_before = called.result["goals_before"]
        assert isinstance(goals_before, list) and goals_before
        assert isinstance(goals_before[0], dict)
        assert goals_before[0]["target"] == "n + 0 = n"

        actions = [
            event_as_dict(record.envelope)
            for record in state.query_events("run-1")
            if record.envelope.event_type == "FormalActionRecorded"
        ]
        assert len(actions) == 5
        action_kinds: set[str] = set()
        for action in actions:
            payload = action["payload"]
            action_kind = payload["action_kind"]
            assert type(action_kind) is str
            action_kinds.add(action_kind)
            assert payload["document_version"] == 0
            assert payload["base_epoch"] == epoch.base_epoch
            assert payload["knowledge_epoch"] == 0
            assert payload["verdict"] == "success"
            assert ".aizim" not in repr(payload)
            assert token not in repr(payload)
        assert action_kinds == {"diagnostics", "goal", "trial"}
    finally:
        await runtime.aclose()
        state.close()
