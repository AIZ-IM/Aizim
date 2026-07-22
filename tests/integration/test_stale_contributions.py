from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from aizim.domain import EpochPair, FileLease, sha256_bytes, sha256_json
from aizim.knowledge import (
    ArtifactStore,
    ByteEdit,
    ContributionDraft,
    ContributionService,
    ContributionValidationError,
    PatchPayload,
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
        assert source and theorem_name.startswith("AizimSmoke.Research.")
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
        StateServiceConfig(tmp_path, "contribution-test"),
        StateDependencies(clock=lambda: _NOW),
    )
    service.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {
                "project_id": "project-1",
                "base_epoch": _BASE,
                "knowledge_epoch": 0,
                "environment_fingerprint": _ENVIRONMENT,
            },
        )
    )
    return service


def _draft(source: bytes, payload: PatchPayload | SnapshotPayload) -> ContributionDraft:
    return ContributionDraft(
        contribution_id="contribution-1",
        worker_id="worker-1",
        run_id="run-1",
        lease_id="lease-1",
        document_id="document-1",
        epoch_pair=EpochPair(_BASE, 0),
        environment_fingerprint=_ENVIRONMENT,
        payload=payload,
        candidate_name="candidate",
        complete_type="True",
        imports=("Std",),
        dependencies=(),
        assumptions=(),
        evidence_links=(),
    )


def _lease(source: bytes) -> FileLease:
    return FileLease(
        lease_id="lease-1",
        worker_id="worker-1",
        run_id="run-1",
        document_id="document-1",
        virtual_document_namespace="AizimSmoke.Workers.W_worker",
        epoch_pair=EpochPair(_BASE, 0),
        file_version=0,
        content_hash=sha256_bytes(source),
        expires_at=_NOW + timedelta(minutes=5),
    )


def test_snapshot_submission_stores_immutable_source(tmp_path: Path) -> None:
    source = b"import Std\ntheorem candidate : True := True.intro\n"
    service = _service(tmp_path)
    try:
        service.grant_document_lease(
            _lease(source), PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean")
        )
        artifacts = ArtifactStore(tmp_path)
        result = ContributionService(service, artifacts, _ENVIRONMENT, ("Std",)).submit(
            _draft(source, SnapshotPayload(source, sha256_bytes(source)))
        )

        assert result.queue_entry.contribution_id == "contribution-1"
        assert result.queue_entry.state.value == "queued"
        assert artifacts.read(result.artifact) == source
        submitted = next(
            record.envelope
            for record in service.query_events("run-1")
            if record.envelope.event_type == "ContributionSubmitted"
        )
        assert submitted.payload["payload_hash"] == sha256_bytes(source)
        assert submitted.payload["payload_kind"] == "snapshot"
    finally:
        service.close()


def test_duplicate_submission_is_idempotent_only_for_identical_content(tmp_path: Path) -> None:
    source = b"import Std\ntheorem candidate : True := True.intro\n"
    service = _service(tmp_path)
    try:
        service.grant_document_lease(
            _lease(source), PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean")
        )
        contributions = ContributionService(
            service, ArtifactStore(tmp_path), _ENVIRONMENT, ("Std",)
        )
        draft = _draft(source, SnapshotPayload(source, sha256_bytes(source)))

        first, second = contributions.submit(draft), contributions.submit(draft)

        assert first.queue_entry == second.queue_entry
        assert (
            len(
                [
                    event
                    for event in service.query_events("run-1")
                    if event.envelope.event_type == "ContributionSubmitted"
                ]
            )
            == 1
        )
        with pytest.raises(ContributionValidationError, match="DUPLICATE_CONTRIBUTION_MISMATCH"):
            contributions.submit(replace(draft, evidence_links=("evidence-1",)))
    finally:
        service.close()


def test_patch_submission_requires_the_current_lease_version(tmp_path: Path) -> None:
    source = b"import Std\ntheorem candidate : True := True.intro\n"
    service = _service(tmp_path)
    try:
        service.grant_document_lease(
            _lease(source), PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean")
        )
        payload = PatchPayload(1, sha256_bytes(source), (ByteEdit(0, 0, b""),))

        with pytest.raises(ContributionValidationError, match="PATCH_FILE_VERSION_MISMATCH"):
            ContributionService(service, ArtifactStore(tmp_path), _ENVIRONMENT, ("Std",)).submit(
                _draft(source, payload), source
            )
    finally:
        service.close()


def test_snapshot_remains_promotable_after_a_later_lease_edit(tmp_path: Path) -> None:
    source = b"import Std\ntheorem candidate : True := True.intro\n"
    later = b"import Std\ntheorem candidate : True := by trivial\n"
    service = _service(tmp_path)
    try:
        service.grant_document_lease(
            _lease(source), PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean")
        )
        artifacts = ArtifactStore(tmp_path)
        ContributionService(service, artifacts, _ENVIRONMENT, ("Std",)).submit(
            _draft(source, SnapshotPayload(source, sha256_bytes(source)))
        )
        prepared = service.prepare_document_edit(
            "run-1", "worker-1", "lease-1", "document-1", 0, sha256_bytes(source)
        )
        service.commit_document_edit(prepared, sha256_bytes(later))

        outcome = asyncio.run(
            PromotionService(service, artifacts, _Verifier(), "owner-1", _Verifier()).promote_next()
        )

        assert outcome is not None and outcome.new_epoch is not None
        assert outcome.rebased_contribution_id is None
    finally:
        service.close()


def test_stale_patch_is_rebased_against_the_current_document_version(tmp_path: Path) -> None:
    source = b"import Std\ntheorem candidate : True := True.intro\n"
    updated = b"import Std\ntheorem candidate : True := by trivial\n"
    service = _service(tmp_path)
    try:
        service.grant_document_lease(
            _lease(source), PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean")
        )
        artifacts = ArtifactStore(tmp_path)
        contribution = ContributionService(service, artifacts, _ENVIRONMENT, ("Std",)).submit(
            _draft(
                source,
                PatchPayload(0, sha256_bytes(source), (ByteEdit(0, 0, b"-- rebased\n"),)),
            ),
            source,
        )
        prepared = service.prepare_document_edit(
            "run-1", "worker-1", "lease-1", "document-1", 0, sha256_bytes(source)
        )
        service.commit_document_edit(prepared, sha256_bytes(updated))
        expected = b"-- rebased\n" + updated
        artifacts.store("run-1", "documents", updated, "text/x-lean")
        promotion = PromotionService(service, artifacts, _Verifier(), "owner-1", _Verifier())

        stale = asyncio.run(promotion.promote_next())
        published = asyncio.run(promotion.promote_next())

        assert stale is not None and stale.rebased_contribution_id is not None
        assert published is not None and published.contribution_id == stale.rebased_contribution_id
        assert published.new_epoch is not None
        submissions = [
            record.envelope.payload
            for record in service.query_events("run-1")
            if record.envelope.event_type == "ContributionSubmitted"
        ]
        original = next(
            payload for payload in submissions if payload.get("contribution_id") == "contribution-1"
        )
        assert original["payload_hash"] == sha256_bytes(b"-- rebased\n" + source)
        assert any(
            payload.get("contribution_id") == stale.rebased_contribution_id
            and payload.get("expected_file_version") == 1
            and payload.get("expected_content_hash") == sha256_bytes(updated)
            and payload.get("payload_hash") == sha256_bytes(expected)
            for payload in submissions
        )
        assert artifacts.load("run-1", "contributions", sha256_bytes(expected)) == expected
        assert contribution.contribution_id != stale.rebased_contribution_id
        assert "ContributionRebased" in [
            record.envelope.event_type for record in service.query_events("run-1")
        ]
    finally:
        service.close()
