from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from aizim.domain import EpochPair, FileLease, sha256_bytes, sha256_json
from aizim.knowledge import (
    ArtifactStore,
    ContributionDraft,
    ContributionService,
    PromotionEvidence,
    PromotionMaterialization,
    PromotionService,
    SnapshotPayload,
)
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


@pytest.mark.parametrize(
    "recovered_state", (PublicationQueueState.VERIFIED, PublicationQueueState.MATERIALIZED)
)
def test_expired_verified_or_materialized_work_is_rechecked_then_published(
    tmp_path: Path, recovered_state: PublicationQueueState
) -> None:
    current = [datetime(2030, 1, 1, tzinfo=UTC)]
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
        claimed = state.claim_next_promotion("crashed-owner")
        assert claimed is not None
        state.advance_promotion(
            claimed.contribution_id, "crashed-owner", PublicationQueueState.VERIFIED
        )
        if recovered_state is PublicationQueueState.MATERIALIZED:
            state.advance_promotion(
                claimed.contribution_id, "crashed-owner", PublicationQueueState.MATERIALIZED
            )
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
