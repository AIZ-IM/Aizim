from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from aizim.domain import EpochPair, FileLease, sha256_bytes
from aizim.lean.documents import (
    BrokerDependencies,
    DocumentBroker,
    DocumentBrokerError,
    DocumentSnapshot,
)
from aizim.lean.project import smoke_base_epoch
from aizim.state import AppendEventCommand, StateDependencies, StateService, StateServiceConfig
from aizim.state.documents import DocumentStateError, LeaseState

NOW = datetime(2026, 7, 21, 10, tzinfo=UTC)
EPOCH = EpochPair("a" * 64, 0)
SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"


def _lease() -> LeaseState:
    return LeaseState(
        lease_id="lease-1",
        worker_id="worker-1",
        run_id="run-1",
        document_id="document-1",
        relative_path=PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean"),
        virtual_document_namespace="AizimSmoke.Workers.run_1.worker_1",
        epoch_pair=EPOCH,
        file_version=0,
        content_hash="b" * 64,
        expires_at=NOW + timedelta(minutes=5),
        active=True,
    )


def test_lease_validates_owner_run_document_expiry_and_epoch() -> None:
    lease = _lease()
    lease.validate_access("run-1", "worker-1", "document-1", NOW, EPOCH)

    cases = (
        (
            lambda: lease.validate_access("other", "worker-1", "document-1", NOW, EPOCH),
            "LEASE_RUN_MISMATCH",
        ),
        (
            lambda: lease.validate_access("run-1", "other", "document-1", NOW, EPOCH),
            "LEASE_OWNER_MISMATCH",
        ),
        (
            lambda: lease.validate_access("run-1", "worker-1", "other", NOW, EPOCH),
            "LEASE_DOCUMENT_MISMATCH",
        ),
        (
            lambda: lease.validate_access(
                "run-1", "worker-1", "document-1", lease.expires_at, EPOCH
            ),
            "LEASE_EXPIRED",
        ),
        (
            lambda: lease.validate_access(
                "run-1", "worker-1", "document-1", NOW, EpochPair("c" * 64, 0)
            ),
            "EPOCH_MISMATCH",
        ),
    )
    for check, code in cases:
        with pytest.raises(DocumentStateError, match=code):
            check()

    with pytest.raises(DocumentStateError, match="LEASE_INACTIVE"):
        replace(lease, active=False).validate_access("run-1", "worker-1", "document-1", NOW, EPOCH)


def test_worker_snapshot_is_immutable_and_path_free() -> None:
    content = b"theorem example : True := trivial\n"
    snapshot = DocumentSnapshot(
        document_id="document-1",
        display_name=PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean"),
        epoch_pair=EPOCH,
        file_version=2,
        content_hash=sha256_bytes(content),
        content=content,
    )
    assert "physical" not in tuple(snapshot.__dataclass_fields__)
    frozen_field = "file_version"
    with pytest.raises(FrozenInstanceError):
        setattr(snapshot, frozen_field, 3)
    with pytest.raises(DocumentBrokerError, match="INVALID_DOCUMENT_SNAPSHOT"):
        replace(snapshot, display_name=PurePosixPath("/private/worker.lean"))


def _service(root: Path, start: int = 0) -> StateService:
    values = iter(f"01J{value:023d}" for value in range(start, start + 20))
    return StateService(
        StateServiceConfig(root, f"session-{start}"),
        StateDependencies(clock=lambda: NOW, event_ids=lambda: next(values)),
    )


def _domain_lease(epoch: EpochPair) -> FileLease:
    body = b"initial\n"
    return FileLease(
        lease_id="lease-state",
        worker_id="worker-state",
        run_id="run-state",
        document_id="document-state",
        virtual_document_namespace="AizimSmoke.Workers.W_state",
        epoch_pair=epoch,
        file_version=0,
        content_hash=sha256_bytes(body),
        expires_at=NOW + timedelta(minutes=5),
        physical_file=None,
    )


def test_state_document_transactions_reject_atomically_and_replay(tmp_path: Path) -> None:
    epoch = EpochPair(smoke_base_epoch(SMOKE_ROOT), 0)
    path = PurePosixPath("AizimSmoke/Workers/run-state/worker-state.lean")
    with _service(tmp_path) as service:
        service.append_event(
            AppendEventCommand(
                "ProjectInitialized",
                "supervisor",
                None,
                None,
                {
                    "project_id": "project-1",
                    "base_epoch": epoch.base_epoch,
                    "knowledge_epoch": 0,
                },
            )
        )
        event_count = len(service.query_events())
        with pytest.raises(DocumentStateError, match="EPOCH_MISMATCH"):
            service.grant_document_lease(_domain_lease(EpochPair("f" * 64, 0)), path)
        with pytest.raises(DocumentStateError, match="LEASE_EXPIRED"):
            service.grant_document_lease(replace(_domain_lease(epoch), expires_at=NOW), path)
        assert len(service.query_events()) == event_count

        lease = _domain_lease(epoch)
        service.grant_document_lease(lease, path)
        event_count = len(service.query_events())
        for version, content_hash, code in (
            (1, lease.content_hash, "DOCUMENT_VERSION_MISMATCH"),
            (0, "f" * 64, "DOCUMENT_HASH_MISMATCH"),
        ):
            with pytest.raises(DocumentStateError, match=code):
                service.prepare_document_edit(
                    lease.run_id,
                    lease.worker_id,
                    lease.lease_id,
                    lease.document_id,
                    version,
                    content_hash,
                )
        assert len(service.query_events()) == event_count

        preparation = service.prepare_document_edit(
            lease.run_id,
            lease.worker_id,
            lease.lease_id,
            lease.document_id,
            0,
            lease.content_hash,
        )
        replacement_hash = sha256_bytes(b"replacement\n")
        service.commit_document_edit(preparation, replacement_hash)
        document = service.document_for(
            lease.run_id, lease.worker_id, lease.lease_id, lease.document_id
        )
        assert (document.file_version, document.content_hash) == (1, replacement_hash)
        service.append_event(
            AppendEventCommand(
                "FormalActionRecorded",
                "lean_runtime",
                lease.run_id,
                None,
                {"action_id": "action-1"},
            )
        )
        assert service.query_projection("formal_actions", "action-1") is not None
        assert service.prepared_documents() == ()
        assert service.replay_verify().matched
        service.append_event(
            AppendEventCommand(
                "KnowledgeDeltaPublished",
                "promotion_service",
                lease.run_id,
                None,
                {
                    "delta_id": "delta-1",
                    "base_epoch": "e" * 64,
                    "knowledge_epoch": 1,
                },
            )
        )
        with pytest.raises(DocumentStateError, match="EPOCH_MISMATCH"):
            service.document_for(lease.run_id, lease.worker_id, lease.lease_id, lease.document_id)


@pytest.mark.asyncio
async def test_broker_restart_restores_prepared_file_and_recovers_lease(
    tmp_path: Path,
) -> None:
    epoch = EpochPair(smoke_base_epoch(SMOKE_ROOT), 0)
    service = _service(tmp_path)
    service.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {
                "project_id": "project-1",
                "base_epoch": epoch.base_epoch,
                "knowledge_epoch": 0,
            },
        )
    )
    dependencies = BrokerDependencies(
        clock=lambda: NOW,
        lease_ids=lambda: "lease-1",
        document_ids=lambda: "document-1",
    )
    broker = DocumentBroker(tmp_path, service, smoke_root=SMOKE_ROOT, dependencies=dependencies)
    path = PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean")
    lease = await broker.create_document("run-1", "worker-1", path, b"committed\n", epoch)
    service.prepare_document_edit(
        "run-1",
        "worker-1",
        lease.lease_id,
        lease.document_id,
        0,
        sha256_bytes(b"committed\n"),
    )
    physical = tmp_path / ".aizim/run/run-1/lean-project" / path
    physical.write_bytes(b"crash-window\n")
    service.close()

    restarted = _service(tmp_path, 100)
    recovered = DocumentBroker(
        tmp_path, restarted, smoke_root=SMOKE_ROOT, dependencies=dependencies
    )
    try:
        with pytest.raises(DocumentBrokerError, match="LEASE_INACTIVE"):
            await recovered.read_document("run-1", "worker-1", lease.lease_id, lease.document_id)
        assert physical.read_bytes() == b"committed\n"
        assert [item.envelope.event_type for item in restarted.query_events()][-2:] == [
            "DocumentEditRecovered",
            "LeaseRecovered",
        ]
        assert restarted.replay_verify().matched
    finally:
        restarted.close()
