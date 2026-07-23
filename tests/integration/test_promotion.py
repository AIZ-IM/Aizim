from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from aizim.domain import EpochPair, FileLease, sha256_bytes, sha256_json
from aizim.knowledge import (
    ArtifactStore,
    ContributionDraft,
    ContributionService,
    KnowledgeReader,
    PromotionEvidence,
    PromotionMaterialization,
    PromotionService,
    SnapshotPayload,
)
from aizim.knowledge.promotion_evidence import record_verification
from aizim.state import (
    AppendEventCommand,
    PublicationQueueState,
    StateDependencies,
    StateService,
    StateServiceConfig,
)

_BASE = "a" * 64
_ENVIRONMENT = "b" * 64
_NOW = datetime(2030, 1, 1, tzinfo=UTC)


class _Verifier:
    async def verify(self, source: bytes, theorem_name: str) -> PromotionEvidence:
        assert b"theorem candidate : True" in source
        assert theorem_name.startswith("AizimSmoke.Research.candidate_")
        return PromotionEvidence(
            (),
            ("propext",),
            "True",
            ("Std",),
            ("propext",),
            axiom_response_hash="c" * 64,
        )

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


class _WarningVerifier(_Verifier):
    async def verify(self, source: bytes, theorem_name: str) -> PromotionEvidence:
        evidence = await super().verify(source, theorem_name)
        return PromotionEvidence(
            evidence.diagnostics,
            evidence.axioms,
            evidence.complete_type,
            evidence.dependencies,
            evidence.assumptions,
            axiom_response_hash=evidence.axiom_response_hash,
            source_scan_warnings=("source scan did not close cleanly",),
        )


def _service(tmp_path: Path) -> StateService:
    service = StateService(
        StateServiceConfig(tmp_path, "promotion-test"), StateDependencies(clock=lambda: _NOW)
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


def test_verified_contribution_publishes_one_atomic_knowledge_delta(tmp_path: Path) -> None:
    source = b"import Std\ntheorem candidate : True := True.intro\n"
    service = _service(tmp_path)
    try:
        lease = FileLease(
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
        service.grant_document_lease(lease, PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean"))
        artifacts = ArtifactStore(tmp_path)
        contribution = ContributionService(service, artifacts, _ENVIRONMENT, ("Std",)).submit(
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
                ("untrusted_dependency",),
                ("untrusted_assumption",),
                (),
            )
        )

        outcome = asyncio.run(
            PromotionService(service, artifacts, _Verifier(), "owner-1", _Verifier()).promote_next()
        )

        assert outcome is not None
        assert outcome.state is PublicationQueueState.PUBLISHED
        assert outcome.contribution_id == contribution.contribution_id
        events = service.query_events("run-1")
        assert [event.envelope.event_type for event in events].count("DeclarationPublished") == 1
        assert [event.envelope.event_type for event in events].count("KnowledgeDeltaPublished") == 1
        verification = next(
            event
            for event in events
            if event.envelope.event_type == "PromotionVerificationRecorded"
        )
        declaration = next(
            event for event in events if event.envelope.event_type == "DeclarationPublished"
        )
        assert verification.sequence < declaration.sequence
        assert verification.envelope.payload["source_scan_verdict"] == "pass"
        assert (
            verification.envelope.payload["source_scan_hash"]
            == verification.envelope.payload["axiom_verification_hash"]
        )
        deltas = KnowledgeReader(service).read(0)
        assert len(deltas) == 1
        assert deltas[0].new_epoch.knowledge_epoch == 1
        assert deltas[0].dependencies == ("Std",)
        assert deltas[0].assumptions == ("propext",)
        assert KnowledgeReader(service).read(1) == ()
        assert KnowledgeReader(service).acknowledge("run-1", "worker-1", deltas[0].delta_id)
        assert not KnowledgeReader(service).acknowledge("run-1", "worker-1", deltas[0].delta_id)
        assert any(
            event.envelope.event_type == "KnowledgeDeltaAcknowledged"
            for event in service.query_events("run-1")
        )
        epochs = service.query_projection("epochs", "global")
        assert epochs is not None
        assert json.loads(epochs.state_json)["knowledge_epoch"] == 1
    finally:
        service.close()


def test_missing_trusted_scan_response_is_recorded_as_failed(tmp_path: Path) -> None:
    # Given
    service = _service(tmp_path)
    evidence = PromotionEvidence((), ("propext",), "True", ("Std",), ("propext",))
    try:
        # When
        record_verification(service, "run-1", "contribution-1", evidence)
        payload = service.query_events("run-1")[-1].envelope.payload

        # Then
        assert payload["source_scan_verdict"] == "failed"
    finally:
        service.close()


def test_source_scan_warnings_are_recorded_as_failed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    evidence = PromotionEvidence(
        (),
        ("propext",),
        "True",
        ("Std",),
        ("propext",),
        axiom_response_hash="c" * 64,
        source_scan_warnings=("source scan did not close cleanly",),
    )
    try:
        record_verification(service, "run-1", "contribution-1", evidence)
        payload = service.query_events("run-1")[-1].envelope.payload

        assert payload["source_scan_verdict"] == "failed"
    finally:
        service.close()


def test_source_scan_warnings_block_publication(tmp_path: Path) -> None:
    source = b"import Std\ntheorem candidate : True := True.intro\n"
    service = _service(tmp_path)
    try:
        lease = FileLease(
            "lease-1",
            "worker-1",
            "run-1",
            "document-1",
            "AizimSmoke.Workers.W_worker",
            EpochPair(_BASE, 0),
            0,
            sha256_bytes(source),
            _NOW + timedelta(minutes=5),
        )
        service.grant_document_lease(lease, PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean"))
        artifacts = ArtifactStore(tmp_path)
        ContributionService(service, artifacts, _ENVIRONMENT, ("Std",)).submit(
            ContributionDraft(
                "contribution-1",
                "worker-1",
                "run-1",
                lease.lease_id,
                lease.document_id,
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

        outcome = asyncio.run(
            PromotionService(
                service, artifacts, _WarningVerifier(), "warning-owner", _Verifier()
            ).promote_next()
        )
        events = service.query_events("run-1")

        assert outcome is not None and outcome.state is PublicationQueueState.QUARANTINED
        assert not any(
            event.envelope.event_type in {"DeclarationPublished", "KnowledgeDeltaPublished"}
            for event in events
        )
        verification = next(
            event.envelope.payload
            for event in events
            if event.envelope.event_type == "PromotionVerificationRecorded"
        )
        failure = next(
            event.envelope.payload
            for event in events
            if event.envelope.event_type == "PromotionFailed"
        )
        artifact_hash = failure["artifact_hash"]
        assert type(artifact_hash) is str
        failure_evidence = json.loads(
            artifacts.load("run-1", "diagnostics", artifact_hash)
        )
        assert verification["source_scan_verdict"] == "failed"
        assert failure_evidence["diagnostics"] == ["source scan did not close cleanly"]
    finally:
        service.close()


def test_recovered_staged_promotion_rechecks_before_publication(tmp_path: Path) -> None:
    current = [_NOW]
    service = StateService(
        StateServiceConfig(tmp_path, "promotion-recovery"),
        StateDependencies(clock=lambda: current[0]),
    )
    source = b"import Std\ntheorem candidate : True := True.intro\n"
    try:
        service.append_event(
            AppendEventCommand(
                "ProjectInitialized",
                "supervisor",
                None,
                None,
                {"project_id": "project-1", "base_epoch": _BASE, "knowledge_epoch": 0},
            )
        )
        service.grant_document_lease(
            FileLease(
                "lease-1",
                "worker-1",
                "run-1",
                "document-1",
                "AizimSmoke.Workers.W_worker",
                EpochPair(_BASE, 0),
                0,
                sha256_bytes(source),
                _NOW + timedelta(minutes=5),
            ),
            PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean"),
        )
        artifacts = ArtifactStore(tmp_path)
        ContributionService(service, artifacts, _ENVIRONMENT, ("Std",)).submit(
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
        assert service.claim_next_promotion("crashed-owner") is not None
        current[0] += timedelta(minutes=2)

        outcome = asyncio.run(
            PromotionService(
                service, artifacts, _Verifier(), "recovery-owner", _Verifier()
            ).recover_next(timedelta(minutes=1))
        )

        assert outcome is not None and outcome.state is PublicationQueueState.PUBLISHED
        assert outcome.new_epoch is not None and outcome.new_epoch.knowledge_epoch == 1
    finally:
        service.close()
