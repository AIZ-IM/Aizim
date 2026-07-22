from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath

from aizim.domain import FileLease

from .document_operations import (
    AccessDocument,
    ActiveLeases,
    CommitEdit,
    DocumentOperation,
    GrantLease,
    PreparedDocuments,
    PrepareEdit,
    RecoverEdit,
    TerminateLease,
)
from .documents import (
    DocumentPreparation,
    DocumentState,
    LeaseState,
    lease_payload,
)
from .events import EventEnvelope
from .operations import AppendEventCommand
from .store_contracts import EventRecord


class DocumentStateMethods:
    def _event(self, command: AppendEventCommand) -> EventEnvelope:
        raise NotImplementedError

    def _document_now(self) -> datetime:
        raise NotImplementedError

    def _execute_document[T](self, operation: DocumentOperation[T]) -> T:
        raise NotImplementedError

    def grant_document_lease(self, lease: FileLease, relative_path: PurePosixPath) -> EventRecord:
        event = self._event(
            AppendEventCommand(
                "LeaseGranted",
                "document_broker",
                lease.run_id,
                None,
                lease_payload(lease, relative_path.as_posix()),
            )
        )
        return self._execute_document(
            GrantLease(lease, relative_path.as_posix(), event.occurred_at, event)
        )

    def document_for(
        self, run_id: str, worker_id: str, lease_id: str, document_id: str
    ) -> DocumentState:
        return self._execute_document(
            AccessDocument(run_id, worker_id, lease_id, document_id, self._document_now())
        )

    def prepare_document_edit(
        self,
        run_id: str,
        worker_id: str,
        lease_id: str,
        document_id: str,
        expected_version: int,
        expected_hash: str,
    ) -> DocumentPreparation:
        access = AccessDocument(run_id, worker_id, lease_id, document_id, self._document_now())
        document = self._execute_document(access)
        payload = document.payload()
        payload["expected_version"] = expected_version
        payload["expected_hash"] = expected_hash
        event = self._event(
            AppendEventCommand(
                "DocumentEditPrepared",
                "document_broker",
                run_id,
                None,
                payload,
            )
        )
        return self._execute_document(PrepareEdit(access, expected_version, expected_hash, event))

    def commit_document_edit(
        self, preparation: DocumentPreparation, replacement_hash: str
    ) -> EventRecord:
        document = preparation.document
        payload = document.payload()
        payload["version"] = document.file_version + 1
        payload["content_hash"] = replacement_hash
        event = self._event(
            AppendEventCommand(
                "DocumentEdited",
                "document_broker",
                document.run_id,
                preparation.preparation_id,
                payload,
            )
        )
        return self._execute_document(
            CommitEdit(preparation.preparation_id, self._document_now(), event)
        )

    def recover_document_edit(self, preparation: DocumentPreparation) -> EventRecord:
        document = preparation.document
        event = self._event(
            AppendEventCommand(
                "DocumentEditRecovered",
                "document_broker",
                document.run_id,
                preparation.preparation_id,
                document.payload(),
            )
        )
        return self._execute_document(RecoverEdit(preparation.preparation_id, event))

    def release_document_lease(self, run_id: str, worker_id: str, lease_id: str) -> EventRecord:
        event = self._event(
            AppendEventCommand(
                "LeaseReleased",
                "document_broker",
                run_id,
                None,
                {"lease_id": lease_id, "reason_code": "RELEASED"},
            )
        )
        return self._execute_document(TerminateLease(run_id, worker_id, lease_id, event))

    def recover_document_lease(self, lease: LeaseState) -> EventRecord:
        event = self._event(
            AppendEventCommand(
                "LeaseRecovered",
                "document_broker",
                lease.run_id,
                None,
                {"lease_id": lease.lease_id, "reason_code": "BROKER_RECOVERY"},
            )
        )
        return self._execute_document(TerminateLease(lease.run_id, None, lease.lease_id, event))

    def active_document_leases(self) -> tuple[LeaseState, ...]:
        return self._execute_document(ActiveLeases())

    def prepared_documents(self) -> tuple[DocumentState, ...]:
        return self._execute_document(PreparedDocuments())
