from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import cast

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
from aizim.state import AppendEventCommand, StateDependencies, StateService, StateServiceConfig

_BASE = "a" * 64
_ENVIRONMENT = "b" * 64
_NOW = datetime(2030, 1, 1, tzinfo=UTC)


class _Verifier:
    async def verify(self, source: bytes, theorem_name: str) -> PromotionEvidence:
        assert theorem_name.startswith("AizimSmoke.Research.")
        assert source.startswith(b"import Std")
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


def _service(tmp_path: Path) -> StateService:
    service = StateService(
        StateServiceConfig(tmp_path, "race-test"), StateDependencies(clock=lambda: _NOW)
    )
    service.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {"project_id": "project-1", "base_epoch": _BASE, "knowledge_epoch": 0},
        )
    )
    return service


def _submit(service: StateService, artifacts: ArtifactStore, index: int) -> None:
    worker, lease_id, document_id = f"worker-{index}", f"lease-{index}", f"document-{index}"
    candidate = f"candidate_{index}"
    source = f"import Std\ntheorem {candidate} : True := True.intro\n".encode()
    service.grant_document_lease(
        FileLease(
            lease_id,
            worker,
            "run-1",
            document_id,
            f"AizimSmoke.Workers.W_{worker}",
            EpochPair(_BASE, 0),
            0,
            sha256_bytes(source),
            _NOW + timedelta(minutes=5),
        ),
        PurePosixPath(f"AizimSmoke/Workers/run-1/{worker}.lean"),
    )
    ContributionService(service, artifacts, _ENVIRONMENT, ("Std",)).submit(
        ContributionDraft(
            f"contribution-{index}",
            worker,
            "run-1",
            lease_id,
            document_id,
            EpochPair(_BASE, 0),
            _ENVIRONMENT,
            SnapshotPayload(source, sha256_bytes(source)),
            candidate,
            "True",
            ("Std",),
            (),
            (),
            (),
        )
    )


def test_racing_snapshots_publish_in_epoch_order_without_lost_update(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        artifacts = ArtifactStore(tmp_path)
        _submit(service, artifacts, 1)
        _submit(service, artifacts, 2)
        promotion = PromotionService(service, artifacts, _Verifier(), "owner-1", _Verifier())

        first = asyncio.run(promotion.promote_next())
        stale = asyncio.run(promotion.promote_next())
        second = asyncio.run(promotion.promote_next())

        assert first is not None and first.new_epoch is not None
        assert stale is not None and stale.rebased_contribution_id is not None
        assert second is not None and second.new_epoch is not None
        assert first.new_epoch.knowledge_epoch == 1
        assert second.new_epoch.knowledge_epoch == 2
        deltas = [
            record.envelope.payload
            for record in service.query_events("run-1")
            if record.envelope.event_type == "KnowledgeDeltaPublished"
        ]
        assert len(deltas) == 2
        delta_ids = [cast(str, delta["delta_id"]) for delta in deltas]
        assert all(type(value) is str for value in delta_ids)
        assert len(set(delta_ids)) == 2
        epochs = service.query_projection("epochs", "global")
        assert epochs is not None and json.loads(epochs.state_json)["knowledge_epoch"] == 2
    finally:
        service.close()
