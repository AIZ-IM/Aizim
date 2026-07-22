from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from aizim.domain import EpochPair, sha256_bytes
from aizim.lean import BrokerDependencies, DocumentBroker
from aizim.lean.models import WorkerSession
from aizim.lean.project import smoke_base_epoch
from aizim.lean.runtime import SharedLeanRuntime
from aizim.state import AppendEventCommand, StateDependencies, StateService, StateServiceConfig

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"


def _source(namespace: str, missing: str) -> bytes:
    return (
        "import Std\n\n"
        f"namespace {namespace}\n\n"
        "theorem goal (n : Nat) : n + 0 = n := by\n"
        "  sorry\n\n"
        f"#check {missing}\n\n"
        f"end {namespace}\n"
    ).encode()


def _path(worker: str) -> PurePosixPath:
    return PurePosixPath(f"AizimSmoke/Workers/run-1/{worker}.lean")


@pytest.mark.lean_integration
@pytest.mark.asyncio
async def test_virtual_attempts_and_diagnostics_do_not_mutate_or_cross_worker_files(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 22, 10, tzinfo=UTC)
    epoch = EpochPair(smoke_base_epoch(SMOKE_ROOT), 0)
    state = StateService(
        StateServiceConfig(tmp_path, "virtual-attempts"), StateDependencies(clock=lambda: now)
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
        first_body = _source("One", "FirstOnlyMissing")
        second_body = _source("Two", "SecondOnlyMissing")
        first = await broker.create_document(
            "run-1", "worker-1", _path("worker-1"), first_body, epoch
        )
        second = await broker.create_document(
            "run-1", "worker-2", _path("worker-2"), second_body, epoch
        )
        first_session = WorkerSession("run-1", "worker-1", first.lease_id)
        second_session = WorkerSession("run-1", "worker-2", second.lease_id)
        physical = tmp_path / ".aizim/run/run-1/lean-project" / _path("worker-1")
        before = (
            physical.read_bytes(),
            physical.stat().st_mtime_ns,
            sha256_bytes(physical.read_bytes()),
        )

        attempts = await runtime.multi_attempt(
            first_session,
            first.document_id,
            6,
            ("rfl", "exact Nat.add_zero n", "exact nope"),
        )
        by_snippet = {item.snippet: item for item in attempts.items}
        assert by_snippet["exact Nat.add_zero n"].diagnostics == ()
        assert by_snippet["exact nope"].diagnostics[0].severity == "error"
        after = (
            physical.read_bytes(),
            physical.stat().st_mtime_ns,
            sha256_bytes(physical.read_bytes()),
        )
        assert after == before

        first_diagnostics = await runtime.diagnostics(first_session, first.document_id)
        second_diagnostics = await runtime.diagnostics(second_session, second.document_id)
        first_text = "\n".join(item.message for item in first_diagnostics.items)
        second_text = "\n".join(item.message for item in second_diagnostics.items)
        assert "FirstOnlyMissing" in first_text and "SecondOnlyMissing" not in first_text
        assert "SecondOnlyMissing" in second_text and "FirstOnlyMissing" not in second_text

        accepted = first_body.replace(b"  sorry", b"  exact Nat.add_zero n")
        updated = await broker.compare_and_swap(
            "run-1",
            "worker-1",
            first.lease_id,
            first.document_id,
            0,
            sha256_bytes(first_body),
            accepted,
        )
        assert updated.file_version == 1
        assert physical.read_bytes() == accepted
        other = tmp_path / ".aizim/run/run-1/lean-project" / _path("worker-2")
        assert other.read_bytes() == second_body
    finally:
        await runtime.aclose()
        state.close()
