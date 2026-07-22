from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from aizim.domain import EpochPair
from aizim.lean import BrokerDependencies, DocumentBroker
from aizim.lean.models import WorkerSession
from aizim.lean.project import smoke_base_epoch
from aizim.lean.runtime import SharedLeanRuntime
from aizim.state import AppendEventCommand, StateDependencies, StateService, StateServiceConfig

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"


def _source(namespace: str) -> bytes:
    return (
        "import Std\n\n"
        f"namespace {namespace}\n\n"
        "theorem goal (n : Nat) : n + 0 = n := by\n"
        "  sorry\n\n"
        f"end {namespace}\n"
    ).encode()


@pytest.mark.lean_integration
@pytest.mark.asyncio
async def test_runtime_restarts_after_the_mcp_process_is_killed(tmp_path: Path) -> None:
    now = datetime(2026, 7, 22, 10, tzinfo=UTC)
    epoch = EpochPair(smoke_base_epoch(SMOKE_ROOT), 0)
    state = StateService(
        StateServiceConfig(tmp_path, "runtime-recovery"), StateDependencies(clock=lambda: now)
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
        path = PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean")
        lease = await broker.create_document("run-1", "worker-1", path, _source("One"), epoch)
        session = WorkerSession("run-1", "worker-1", lease.lease_id)
        before = await runtime.goal(session, lease.document_id, 6)
        first_pid = runtime.mcp_process_id
        await runtime._terminate_for_test()
        after = await runtime.goal(session, lease.document_id, 6)

        assert after.goals_before == before.goals_before
        assert runtime.mcp_process_id != first_pid
        events = [record.envelope.event_type for record in state.query_events("run-1")]
        assert events.count("LeanRuntimeStarted") == 1
        assert events.count("LeanRuntimeCrashed") == 1
        assert events.count("LeanRuntimeRestarted") == 1
    finally:
        await runtime.aclose()
        state.close()
