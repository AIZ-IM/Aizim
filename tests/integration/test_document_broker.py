from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from aizim.domain import EpochPair, sha256_bytes
from aizim.lean.documents import (
    BrokerDependencies,
    DocumentBroker,
    DocumentBrokerError,
)
from aizim.lean.project import smoke_base_epoch
from aizim.state import AppendEventCommand, StateDependencies, StateService, StateServiceConfig

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"


class SequenceIds:
    def __init__(self, prefix: str = "01J", start: int = 0) -> None:
        self.prefix = prefix
        self.value = start

    def __call__(self) -> str:
        value = self.value
        self.value += 1
        if self.prefix == "01J":
            return f"01J{value:023d}"
        return f"{self.prefix}-{value}"


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 21, 10, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _path(run_id: str, worker_id: str) -> PurePosixPath:
    return PurePosixPath(f"AizimSmoke/Workers/{run_id}/{worker_id}.lean")


def _open(tmp_path: Path) -> tuple[StateService, DocumentBroker, MutableClock, EpochPair]:
    clock = MutableClock()
    epoch = EpochPair(smoke_base_epoch(SMOKE_ROOT), 0)
    service = StateService(
        StateServiceConfig(tmp_path, "service-session"),
        StateDependencies(clock=clock, event_ids=SequenceIds()),
    )
    service.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {
                "project_id": "project-1",
                "base_epoch": epoch.base_epoch,
                "knowledge_epoch": epoch.knowledge_epoch,
            },
        )
    )
    broker = DocumentBroker(
        tmp_path,
        service,
        smoke_root=SMOKE_ROOT,
        dependencies=BrokerDependencies(
            clock=clock,
            lease_ids=SequenceIds("lease"),
            document_ids=SequenceIds("document"),
            lease_lifetime=timedelta(minutes=5),
        ),
    )
    return service, broker, clock, epoch


@pytest.mark.asyncio
async def test_two_workers_get_distinct_path_free_leases_and_documents(
    tmp_path: Path,
) -> None:
    service, broker, _clock, epoch = _open(tmp_path)
    try:
        first = await broker.create_document(
            "run-1", "worker-1", _path("run-1", "worker-1"), b"first\n", epoch
        )
        second = await broker.create_document(
            "run-1", "worker-2", _path("run-1", "worker-2"), b"second\n", epoch
        )
        assert first.lease_id != second.lease_id
        assert first.document_id != second.document_id
        assert first.virtual_document_namespace != second.virtual_document_namespace
        assert first.physical_file is None is second.physical_file
        first_file = tmp_path / ".aizim/run/run-1/lean-project" / _path("run-1", "worker-1")
        second_file = tmp_path / ".aizim/run/run-1/lean-project" / _path("run-1", "worker-2")
        assert first_file.read_bytes() == b"first\n"
        assert second_file.read_bytes() == b"second\n"
        updated = await broker.compare_and_swap(
            "run-1",
            "worker-1",
            first.lease_id,
            first.document_id,
            0,
            sha256_bytes(b"first\n"),
            b"first-updated\n",
        )
        assert updated.file_version == 1
        assert first_file.read_bytes() == b"first-updated\n"
        assert second_file.read_bytes() == b"second\n"
        assert (
            tmp_path / ".aizim/artifacts/run-1/documents" / sha256_bytes(b"first-updated\n")
        ).read_bytes() == b"first-updated\n"

        with pytest.raises(DocumentBrokerError, match="DOCUMENT_ALREADY_LEASED"):
            await broker.create_document(
                "run-1", "worker-1", _path("run-1", "worker-1"), b"again", epoch
            )
    finally:
        service.close()


@pytest.mark.asyncio
async def test_cas_validates_state_and_serializes_same_version_race(
    tmp_path: Path,
) -> None:
    service, broker, clock, epoch = _open(tmp_path)
    try:
        lease = await broker.create_document(
            "run-1", "worker-1", _path("run-1", "worker-1"), b"old\n", epoch
        )
        old_hash = sha256_bytes(b"old\n")
        event_count = len(service.query_events())
        for version, content_hash, code in (
            (1, old_hash, "DOCUMENT_VERSION_MISMATCH"),
            (0, "f" * 64, "DOCUMENT_HASH_MISMATCH"),
        ):
            with pytest.raises(DocumentBrokerError, match=code):
                await broker.compare_and_swap(
                    "run-1",
                    "worker-1",
                    lease.lease_id,
                    lease.document_id,
                    version,
                    content_hash,
                    b"rejected\n",
                )
        assert len(service.query_events()) == event_count
        assert (
            await broker.read_document("run-1", "worker-1", lease.lease_id, lease.document_id)
        ).content == b"old\n"
        attempts = await asyncio.gather(
            broker.compare_and_swap(
                "run-1",
                "worker-1",
                lease.lease_id,
                lease.document_id,
                0,
                old_hash,
                b"winner-one\n",
            ),
            broker.compare_and_swap(
                "run-1",
                "worker-1",
                lease.lease_id,
                lease.document_id,
                0,
                old_hash,
                b"winner-two\n",
            ),
            return_exceptions=True,
        )
        snapshots = [item for item in attempts if not isinstance(item, BaseException)]
        failures = [item for item in attempts if isinstance(item, BaseException)]
        assert len(snapshots) == len(failures) == 1
        assert snapshots[0].file_version == 1
        assert "DOCUMENT_VERSION_MISMATCH" in str(failures[0])

        with pytest.raises(DocumentBrokerError, match="LEASE_OWNER_MISMATCH"):
            await broker.read_document("run-1", "worker-2", lease.lease_id, lease.document_id)
        with pytest.raises(DocumentBrokerError, match="LEASE_RUN_MISMATCH"):
            await broker.read_document("run-2", "worker-1", lease.lease_id, lease.document_id)
        with pytest.raises(DocumentBrokerError, match="LEASE_NOT_FOUND"):
            await broker.read_document("run-1", "worker-1", "missing-lease", lease.document_id)
        clock.now += timedelta(minutes=5)
        with pytest.raises(DocumentBrokerError, match="LEASE_EXPIRED"):
            await broker.read_document("run-1", "worker-1", lease.lease_id, lease.document_id)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_failed_state_commit_restores_bytes_and_records_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, broker, _clock, epoch = _open(tmp_path)
    try:
        lease = await broker.create_document(
            "run-1", "worker-1", _path("run-1", "worker-1"), b"stable\n", epoch
        )

        def fail_commit(*_arguments: object) -> None:
            raise RuntimeError("injected final state failure")

        monkeypatch.setattr(service, "commit_document_edit", fail_commit)
        with pytest.raises(DocumentBrokerError, match="DOCUMENT_STATE_COMMIT_FAILED"):
            await broker.compare_and_swap(
                "run-1",
                "worker-1",
                lease.lease_id,
                lease.document_id,
                0,
                sha256_bytes(b"stable\n"),
                b"uncommitted\n",
            )
        snapshot = await broker.read_document(
            "run-1", "worker-1", lease.lease_id, lease.document_id
        )
        assert snapshot.content == b"stable\n"
        assert snapshot.file_version == 0
        assert [item.envelope.event_type for item in service.query_events()][-2:] == [
            "DocumentEditPrepared",
            "DocumentEditRecovered",
        ]

        def fail_recovery(*_arguments: object) -> None:
            raise RuntimeError("injected recovery failure")

        monkeypatch.setattr(service, "recover_document_edit", fail_recovery)
        with pytest.raises(DocumentBrokerError, match="DOCUMENT_RECOVERY_FAILED"):
            await broker.compare_and_swap(
                "run-1",
                "worker-1",
                lease.lease_id,
                lease.document_id,
                0,
                sha256_bytes(b"stable\n"),
                b"still-uncommitted\n",
            )
        physical = tmp_path / ".aizim/run/run-1/lean-project" / _path("run-1", "worker-1")
        assert physical.read_bytes() == b"stable\n"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_release_preserves_snapshot_and_revokes_edits(tmp_path: Path) -> None:
    service, broker, _clock, epoch = _open(tmp_path)
    try:
        lease = await broker.create_document(
            "run-1", "worker-1", _path("run-1", "worker-1"), b"committed\n", epoch
        )
        await broker.release_lease("run-1", "worker-1", lease.lease_id)
        digest = sha256_bytes(b"committed\n")
        snapshot = tmp_path / ".aizim/artifacts/run-1/documents" / digest
        assert snapshot.read_bytes() == b"committed\n"
        assert snapshot.stat().st_mode & 0o777 == 0o600
        with pytest.raises(DocumentBrokerError, match="LEASE_INACTIVE"):
            await broker.compare_and_swap(
                "run-1",
                "worker-1",
                lease.lease_id,
                lease.document_id,
                0,
                digest,
                b"forbidden\n",
            )
    finally:
        service.close()
