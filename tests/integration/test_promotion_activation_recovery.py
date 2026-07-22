from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from aizim.domain import EpochPair, sha256_bytes
from aizim.knowledge import (
    ArtifactStore,
    ContributionDraft,
    ContributionService,
    PromotionMaterialization,
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


class _InterruptedActivation:
    def __init__(self, verifier: RuntimePromotionVerifier) -> None:
        self._verifier = verifier

    async def verify(self, source: bytes, theorem_name: str):
        return await self._verifier.verify(source, theorem_name)

    async def materialize(
        self, source: bytes, epoch_pair: EpochPair, publication_sequence: int
    ) -> PromotionMaterialization:
        return await self._verifier.materialize(source, epoch_pair, publication_sequence)

    async def activate(self, materialization: PromotionMaterialization) -> None:
        await self._verifier.activate(materialization)
        raise asyncio.CancelledError


@pytest.mark.lean_integration
@pytest.mark.asyncio
async def test_restart_removes_an_activated_manifest_without_a_published_declaration(
    tmp_path: Path,
) -> None:
    current = [datetime(2026, 7, 22, 10, tzinfo=UTC)]
    epoch = EpochPair(smoke_base_epoch(_SMOKE_ROOT), 0)
    state = StateService(
        StateServiceConfig(tmp_path, "activation-before-restart"),
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
        verifier = RuntimePromotionVerifier(runtime, broker, "run-1")
        interrupted = _InterruptedActivation(verifier)

        with pytest.raises(asyncio.CancelledError):
            await PromotionService(
                state, artifacts, interrupted, "owner-a", interrupted
            ).promote_next()

        prepared = next(
            record.envelope.payload
            for record in state.query_events("run-1")
            if record.envelope.event_type == "PromotionPrepared"
        )
        module = prepared["module"]
        assert type(module) is str
        manifest = tmp_path / ".aizim" / "run" / "run-1" / "lean-project" / "AizimSmoke.lean"
        assert f"import {module}\n".encode() in manifest.read_bytes()
        assert not any(
            record.envelope.event_type == "DeclarationPublished"
            for record in state.query_events("run-1")
        )
        await runtime.aclose()
        state.close()
        current[0] += timedelta(minutes=2)

        restarted = StateService(
            StateServiceConfig(tmp_path, "activation-after-restart"),
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
            project = await restarted_broker._trusted_promotion_project("run-1")
            assert f"import {module}\n".encode() not in (project / "AizimSmoke.lean").read_bytes()
            recovery = RuntimePromotionVerifier(restarted_runtime, restarted_broker, "run-1")
            outcome = await PromotionService(
                restarted, artifacts, recovery, "owner-b", recovery
            ).recover_next(timedelta(minutes=1))

            assert outcome is not None and outcome.state is PublicationQueueState.PUBLISHED
            assert f"import {module}\n".encode() in (project / "AizimSmoke.lean").read_bytes()
        finally:
            await restarted_runtime.aclose()
            restarted.close()
    except BaseException:
        await runtime.aclose()
        state.close()
        raise
