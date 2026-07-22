from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

from aizim.domain import EpochPair, sha256_bytes
from aizim.knowledge import (
    ArtifactStore,
    ContributionDraft,
    ContributionService,
    PromotionService,
    RuntimePromotionVerifier,
    SnapshotPayload,
)
from aizim.lean import BrokerDependencies, DocumentBroker, SharedLeanRuntime
from aizim.lean.project import smoke_base_epoch
from aizim.state import (
    AppendEventCommand,
    PublicationQueueState,
    StateDependencies,
    StateService,
    StateServiceConfig,
)

_SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"
_ENVIRONMENT = "b" * 64


@pytest.mark.lean_integration
@pytest.mark.asyncio
async def test_promotion_uses_the_trusted_runtime_for_diagnostics_build_and_axioms(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 22, 10, tzinfo=UTC)
    epoch = EpochPair(smoke_base_epoch(_SMOKE_ROOT), 0)
    state = StateService(
        StateServiceConfig(tmp_path, "real-promotion"), StateDependencies(clock=lambda: now)
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
        smoke_root=_SMOKE_ROOT,
        dependencies=BrokerDependencies(clock=lambda: now, lease_lifetime=timedelta(minutes=5)),
    )
    runtime = SharedLeanRuntime(state, broker, "run-1")
    source = b"import Std\n\ntheorem candidate : True := by\n  trivial\n\n"
    try:
        lease = await broker.create_document(
            "run-1",
            "worker-1",
            PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean"),
            source,
            epoch,
        )
        artifacts = ArtifactStore(tmp_path)
        ContributionService(state, artifacts, _ENVIRONMENT, ("Std",)).submit(
            ContributionDraft(
                "contribution-1",
                "worker-1",
                "run-1",
                lease.lease_id,
                lease.document_id,
                epoch,
                _ENVIRONMENT,
                SnapshotPayload(source, sha256_bytes(source)),
                "candidate",
                "True",
                ("Std",),
                (),
                (),
                (),
            )
        )

        verifier = RuntimePromotionVerifier(runtime, broker, "run-1")
        outcome = await PromotionService(
            state,
            artifacts,
            verifier,
            "promotion-owner",
            materializer=verifier,
        ).promote_next()

        assert outcome is not None and outcome.state is PublicationQueueState.PUBLISHED
        assert outcome.new_epoch is not None and outcome.new_epoch.knowledge_epoch == 1
        declaration = next(
            record.envelope.payload
            for record in state.query_events("run-1")
            if record.envelope.event_type == "DeclarationPublished"
        )
        axioms = declaration["axioms"]
        assert type(axioms) is tuple
        assert "sorryAx" not in cast(tuple[str, ...], axioms)
        assert declaration["type"] == "True"
        assert declaration["dependencies"] == ("Std",)
        assert declaration["assumptions"] == axioms
        module = declaration["module"]
        assert type(module) is str
        run_project = tmp_path / ".aizim" / "run" / "run-1" / "lean-project"
        module_path = run_project / f"{module.replace('.', '/')}.lean"
        assert module_path.is_file()
        assert f"import {module}\n".encode() in (run_project / "AizimSmoke.lean").read_bytes()
        assert (await runtime._build()).success
        next_lease = await broker.create_document(
            "run-2",
            "worker-2",
            PurePosixPath("AizimSmoke/Workers/run-2/worker-2.lean"),
            f"import {module}\ntheorem consumer : True := True.intro\n".encode(),
            outcome.new_epoch,
        )
        next_project = tmp_path / ".aizim" / "run" / "run-2" / "lean-project"
        assert next_lease.epoch_pair == outcome.new_epoch
        assert f"import {module}\n".encode() in (next_project / "AizimSmoke.lean").read_bytes()
        assert (next_project / f"{module.replace('.', '/')}.lean").is_file()
    finally:
        await runtime.aclose()
        state.close()


@pytest.mark.lean_integration
@pytest.mark.asyncio
async def test_recovery_verifies_without_the_expired_worker_lease(tmp_path: Path) -> None:
    current = [datetime(2026, 7, 22, 10, tzinfo=UTC)]
    epoch = EpochPair(smoke_base_epoch(_SMOKE_ROOT), 0)
    state = StateService(
        StateServiceConfig(tmp_path, "promotion-before-restart"),
        StateDependencies(clock=lambda: current[0]),
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
        smoke_root=_SMOKE_ROOT,
        dependencies=BrokerDependencies(
            clock=lambda: current[0], lease_lifetime=timedelta(minutes=5)
        ),
    )
    runtime = SharedLeanRuntime(state, broker, "run-1")
    source = b"import Std\ntheorem candidate : True := True.intro\n"
    try:
        lease = await broker.create_document(
            "run-1",
            "worker-1",
            PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean"),
            source,
            epoch,
        )
        artifacts = ArtifactStore(tmp_path)
        ContributionService(state, artifacts, _ENVIRONMENT, ("Std",)).submit(
            ContributionDraft(
                "contribution-1",
                "worker-1",
                "run-1",
                lease.lease_id,
                lease.document_id,
                epoch,
                _ENVIRONMENT,
                SnapshotPayload(source, sha256_bytes(source)),
                "candidate",
                "True",
                ("Std",),
                (),
                (),
                (),
            )
        )
        assert state.claim_next_promotion("crashed-owner") is not None
        await runtime.aclose()
        state.close()
        current[0] += timedelta(minutes=2)

        restarted = StateService(
            StateServiceConfig(tmp_path, "promotion-after-restart"),
            StateDependencies(clock=lambda: current[0]),
        )
        restarted_broker = DocumentBroker(
            tmp_path,
            restarted,
            smoke_root=_SMOKE_ROOT,
            dependencies=BrokerDependencies(
                clock=lambda: current[0], lease_lifetime=timedelta(minutes=5)
            ),
        )
        restarted_runtime = SharedLeanRuntime(restarted, restarted_broker, "run-1")
        try:
            verifier = RuntimePromotionVerifier(restarted_runtime, restarted_broker, "run-1")
            outcome = await PromotionService(
                restarted, artifacts, verifier, "recovery-owner", verifier
            ).recover_next(timedelta(minutes=1))

            assert outcome is not None and outcome.state is PublicationQueueState.PUBLISHED
            assert any(
                record.envelope.event_type == "LeaseRecovered"
                for record in restarted.query_events("run-1")
            )
        finally:
            await restarted_runtime.aclose()
            restarted.close()
    except BaseException:
        await runtime.aclose()
        state.close()
        raise
