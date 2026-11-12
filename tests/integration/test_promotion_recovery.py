from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

from aizim.domain import EpochPair, FileLease, sha256_bytes, sha256_json
from aizim.knowledge import (
    ArtifactStore,
    ContributionDraft,
    ContributionService,
    PromotionEvidence,
    PromotionMaterialization,
    PromotionService,
    RuntimePromotionVerifier,
    SnapshotPayload,
)
from aizim.orchestration.knowledge_stream import KnowledgeStream
from aizim.orchestration.promotion_consumer import PromotionConsumer
from aizim.state import (
    AppendEventCommand,
    PublicationQueueState,
    StateDependencies,
    StateService,
    StateServiceConfig,
)

_BASE = "a" * 64
_ENVIRONMENT = "b" * 64


class _Verifier:
    def __init__(self) -> None:
        self.calls = 0

    async def verify(self, source: bytes, theorem_name: str) -> PromotionEvidence:
        self.calls += 1
        assert source.startswith(b"import Std")
        assert theorem_name.startswith("AizimSmoke.Research.candidate_")
        return PromotionEvidence((), (), "True", ("Std",), ())

    async def materialize(
        self, source: bytes, epoch_pair: EpochPair, publication_sequence: int
    ) -> PromotionMaterialization:
        digest = sha256_bytes(source)
        return PromotionMaterialization(
            f"AizimSmoke.Research.M{publication_sequence}_{digest[:16]}",
            digest,
            sha256_json({"previous": epoch_pair.base_epoch, "module": digest}),
        )

    async def activate(self, materialization: PromotionMaterialization) -> None:
        del materialization
        return None


def _crashed_promotion(
    tmp_path: Path, current: list[datetime], crashed_state: PublicationQueueState
) -> tuple[StateService, ArtifactStore]:
    """Queue one contribution, optionally claimed by an owner that stopped heartbeating."""
    source = b"import Std\ntheorem candidate : True := True.intro\n"
    state = StateService(
        StateServiceConfig(tmp_path, "promotion-recovery"),
        StateDependencies(clock=lambda: current[0]),
    )
    try:
        state.append_event(
            AppendEventCommand(
                "ProjectInitialized",
                "supervisor",
                None,
                None,
                {"project_id": "project-1", "base_epoch": _BASE, "knowledge_epoch": 0},
            )
        )
        state.grant_document_lease(
            FileLease(
                "lease-1",
                "worker-1",
                "run-1",
                "document-1",
                "AizimSmoke.Workers.W_worker",
                EpochPair(_BASE, 0),
                0,
                sha256_bytes(source),
                current[0] + timedelta(minutes=5),
            ),
            PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean"),
        )
        artifacts = ArtifactStore(tmp_path)
        ContributionService(state, artifacts, _ENVIRONMENT, ("Std",)).submit(
            ContributionDraft(
                "contribution-1",
                "worker-1",
                "run-1",
                "lease-1",
                "document-1",
                EpochPair(_BASE, 0),
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
        if crashed_state is PublicationQueueState.QUEUED:
            return state, artifacts
        claimed = state.claim_next_promotion("crashed-owner")
        assert claimed is not None
        for step in (PublicationQueueState.VERIFIED, PublicationQueueState.MATERIALIZED):
            if claimed.state is crashed_state:
                break
            claimed = state.advance_promotion(claimed.contribution_id, "crashed-owner", step)
    except BaseException:
        state.close()
        raise
    return state, artifacts


@pytest.mark.parametrize(
    "recovered_state", (PublicationQueueState.VERIFIED, PublicationQueueState.MATERIALIZED)
)
def test_expired_verified_or_materialized_work_is_rechecked_then_published(
    tmp_path: Path, recovered_state: PublicationQueueState
) -> None:
    current = [datetime(2030, 1, 1, tzinfo=UTC)]
    state, artifacts = _crashed_promotion(tmp_path, current, recovered_state)
    try:
        current[0] += timedelta(minutes=2)
        verifier = _Verifier()

        outcome = asyncio.run(
            PromotionService(state, artifacts, verifier, "recovery-owner", verifier).recover_next(
                timedelta(minutes=1)
            )
        )

        assert outcome is not None and outcome.state is PublicationQueueState.PUBLISHED
        assert outcome.new_epoch is not None and outcome.new_epoch.knowledge_epoch == 1
        assert verifier.calls == 1
    finally:
        state.close()


@pytest.mark.parametrize("continue_on_failure", (False, True))
@pytest.mark.parametrize(
    "claimed_state",
    (
        PublicationQueueState.STAGED,
        PublicationQueueState.VERIFIED,
        PublicationQueueState.MATERIALIZED,
    ),
)
async def test_every_consumer_takes_over_a_promotion_whose_owner_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    continue_on_failure: bool,
    claimed_state: PublicationQueueState,
) -> None:
    monkeypatch.setattr("aizim.orchestration.promotion_consumer._RECOVERY_POLL_SECONDS", 0.01)
    current = [datetime(2030, 1, 1, tzinfo=UTC)]
    state, artifacts = _crashed_promotion(tmp_path, current, claimed_state)
    verifier = _Verifier()
    service = PromotionService(state, artifacts, verifier, "shared-promotion-consumer", verifier)
    consumer = PromotionConsumer(
        state,
        artifacts,
        cast(RuntimePromotionVerifier, object()),
        KnowledgeStream(state),
        continue_on_failure=continue_on_failure,
    )
    monkeypatch.setattr(consumer, "_service", lambda: service)
    stop = asyncio.Event()
    worker = asyncio.create_task(consumer.run(stop))
    try:
        # No submission in this process announces the claim, and a fresh claim may be live.
        await asyncio.sleep(0.1)
        assert verifier.calls == 0
        current[0] += timedelta(minutes=2)
        await asyncio.wait_for(consumer.wait_for_epoch(1), timeout=2)
        assert verifier.calls == 1
    finally:
        stop.set()
        consumer.notify_submission()
        await worker
        state.close()


@pytest.mark.parametrize("continue_on_failure", (False, True))
async def test_every_consumer_drains_queued_work_on_startup_without_a_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, continue_on_failure: bool
) -> None:
    # Startup recovery must not wait for the periodic poll or a new submission.
    monkeypatch.setattr("aizim.orchestration.promotion_consumer._RECOVERY_POLL_SECONDS", 60.0)
    current = [datetime(2030, 1, 1, tzinfo=UTC)]
    state, artifacts = _crashed_promotion(tmp_path, current, PublicationQueueState.QUEUED)
    verifier = _Verifier()
    service = PromotionService(state, artifacts, verifier, "startup-consumer", verifier)
    consumer = PromotionConsumer(
        state,
        artifacts,
        cast(RuntimePromotionVerifier, object()),
        KnowledgeStream(state),
        continue_on_failure=continue_on_failure,
    )
    monkeypatch.setattr(consumer, "_service", lambda: service)
    stop = asyncio.Event()
    worker = asyncio.create_task(consumer.run(stop))
    try:
        await asyncio.wait_for(consumer.wait_for_epoch(1), timeout=2)
        assert verifier.calls == 1
    finally:
        # The host's stop-and-wake protocol must interrupt even a long recovery poll.
        stop.set()
        consumer.notify_submission()
        try:
            await asyncio.wait_for(worker, timeout=2)
        finally:
            state.close()


@pytest.mark.parametrize("continue_on_failure", (False, True))
async def test_stopped_consumer_leaves_queued_work_for_the_next_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, continue_on_failure: bool
) -> None:
    current = [datetime(2030, 1, 1, tzinfo=UTC)]
    state, artifacts = _crashed_promotion(tmp_path, current, PublicationQueueState.QUEUED)
    verifier = _Verifier()
    service = PromotionService(state, artifacts, verifier, "stopped-consumer", verifier)
    consumer = PromotionConsumer(
        state,
        artifacts,
        cast(RuntimePromotionVerifier, object()),
        KnowledgeStream(state),
        continue_on_failure=continue_on_failure,
    )
    monkeypatch.setattr(consumer, "_service", lambda: service)
    stop = asyncio.Event()
    stop.set()
    consumer.notify_submission()
    try:
        await asyncio.wait_for(consumer.run(stop), timeout=2)
        assert verifier.calls == 0
        claimed = state.claim_next_promotion("next-owner")
        assert claimed is not None and claimed.contribution_id == "contribution-1"
    finally:
        state.close()
