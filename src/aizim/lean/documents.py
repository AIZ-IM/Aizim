from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath

from aizim.domain import EpochPair, FileLease, sha256_bytes
from aizim.state.document_service import DocumentStateMethods
from aizim.state.documents import (
    DocumentPreparation,
    DocumentState,
    DocumentStateError,
)

from .broker_storage import DocumentStorage
from .document_io import DocumentIoError
from .models import BrokerDependencies, DocumentBrokerError, DocumentSnapshot
from .path_policy import LeanPathError
from .project import smoke_base_epoch


class DocumentBroker:
    def __init__(
        self,
        project_root: Path,
        state: DocumentStateMethods,
        *,
        smoke_root: Path,
        dependencies: BrokerDependencies | None = None,
    ) -> None:
        self._state = state
        self._storage = DocumentStorage(project_root, smoke_root)
        self._base_epoch = smoke_base_epoch(smoke_root)
        self._dependencies = BrokerDependencies() if dependencies is None else dependencies
        self._locks: dict[str, asyncio.Lock] = {}
        self._recovery_lock = asyncio.Lock()
        self._recovered = False

    async def create_document(
        self,
        run_id: str,
        worker_id: str,
        relative_path: PurePosixPath,
        initial_content: bytes,
        epoch_pair: EpochPair,
    ) -> FileLease:
        await self._ensure_recovered()
        try:
            canonical = self._storage.canonical_document(run_id, worker_id, relative_path)
        except (DocumentIoError, LeanPathError) as error:
            raise DocumentBrokerError(str(error)) from None
        if type(initial_content) is not bytes or type(epoch_pair) is not EpochPair:
            raise DocumentBrokerError("INVALID_DOCUMENT_REQUEST")
        if epoch_pair.base_epoch != self._base_epoch:
            raise DocumentBrokerError("EPOCH_MISMATCH")
        created = False
        try:
            body_hash = self._storage.create(run_id, canonical, initial_content)
            created = True
            lease = self._lease(run_id, worker_id, canonical, epoch_pair, body_hash)
            self._state.grant_document_lease(lease, canonical)
            return lease
        except (DocumentIoError, LeanPathError, DocumentStateError) as error:
            if created:
                self._discard_or_raise(run_id, canonical)
            raise DocumentBrokerError(str(error)) from None
        except Exception:
            if created:
                self._discard_or_raise(run_id, canonical)
            raise DocumentBrokerError("DOCUMENT_CREATION_FAILED") from None

    async def read_document(
        self, run_id: str, worker_id: str, lease_id: str, document_id: str
    ) -> DocumentSnapshot:
        await self._ensure_recovered()
        try:
            document = self._state.document_for(run_id, worker_id, lease_id, document_id)
            body = self._storage.read(document)
        except (DocumentIoError, LeanPathError, DocumentStateError) as error:
            raise DocumentBrokerError(str(error)) from None
        except Exception:
            raise DocumentBrokerError("DOCUMENT_READ_FAILED") from None
        if sha256_bytes(body) != document.content_hash:
            raise DocumentBrokerError("DOCUMENT_CONTENT_MISMATCH")
        return self._snapshot(document, body)

    async def compare_and_swap(
        self,
        run_id: str,
        worker_id: str,
        lease_id: str,
        document_id: str,
        expected_version: int,
        expected_hash: str,
        replacement: bytes,
    ) -> DocumentSnapshot:
        await self._ensure_recovered()
        if type(replacement) is not bytes:
            raise DocumentBrokerError("INVALID_DOCUMENT_REQUEST")
        lock = self._locks.setdefault(document_id, asyncio.Lock())
        async with lock:
            preparation: DocumentPreparation | None = None
            replaced = False
            try:
                preparation = self._state.prepare_document_edit(
                    run_id,
                    worker_id,
                    lease_id,
                    document_id,
                    expected_version,
                    expected_hash,
                )
                previous = self._storage.read(preparation.document)
                if sha256_bytes(previous) != preparation.document.content_hash:
                    raise DocumentBrokerError("DOCUMENT_CONTENT_MISMATCH")
                self._storage.store_snapshot(run_id, preparation.document.content_hash, previous)
                replacement_hash = sha256_bytes(replacement)
                self._storage.store_snapshot(run_id, replacement_hash, replacement)
                replaced = True
                self._storage.replace(preparation.document, replacement)
                snapshot = self._snapshot(
                    _committed(preparation.document, replacement_hash), replacement
                )
                self._state.commit_document_edit(preparation, replacement_hash)
                return snapshot
            except asyncio.CancelledError:
                self._compensate_or_raise(preparation, replaced)
                raise
            except (DocumentIoError, LeanPathError, DocumentStateError) as error:
                self._compensate_or_raise(preparation, replaced)
                raise DocumentBrokerError(str(error)) from None
            except DocumentBrokerError:
                self._compensate_or_raise(preparation, replaced)
                raise
            except Exception:
                self._compensate_or_raise(preparation, replaced)
                raise DocumentBrokerError("DOCUMENT_STATE_COMMIT_FAILED") from None

    async def release_lease(self, run_id: str, worker_id: str, lease_id: str) -> None:
        await self._ensure_recovered()
        try:
            self._state.release_document_lease(run_id, worker_id, lease_id)
        except DocumentStateError as error:
            raise DocumentBrokerError(str(error)) from None
        except Exception:
            raise DocumentBrokerError("DOCUMENT_RELEASE_FAILED") from None

    async def _trusted_runtime_document(
        self, run_id: str, worker_id: str, lease_id: str, document_id: str
    ) -> tuple[DocumentState, Path, Path]:
        await self._ensure_recovered()
        try:
            document = self._state.document_for(run_id, worker_id, lease_id, document_id)
            project_root, path = self._storage.resolved_path(document)
        except (DocumentIoError, LeanPathError, DocumentStateError) as error:
            raise DocumentBrokerError(str(error)) from None
        return document, project_root, path

    async def _ensure_recovered(self) -> None:
        if self._recovered:
            return
        async with self._recovery_lock:
            if self._recovered:
                return
            try:
                for document in self._state.prepared_documents():
                    self._storage.register(document)
                    self._storage.restore(document)
                    self._state.recover_document_edit(
                        DocumentPreparation(document.prepared_event_id or "", document)
                    )
                for lease in self._state.active_document_leases():
                    self._state.recover_document_lease(lease)
            except (DocumentIoError, LeanPathError, DocumentStateError) as error:
                raise DocumentBrokerError(str(error)) from None
            except Exception:
                raise DocumentBrokerError("DOCUMENT_RECOVERY_FAILED") from None
            self._recovered = True

    def _lease(
        self,
        run_id: str,
        worker_id: str,
        relative: PurePosixPath,
        epoch_pair: EpochPair,
        content_hash: str,
    ) -> FileLease:
        namespace_hash = sha256_bytes(f"{run_id}\0{worker_id}".encode())[:20]
        return FileLease(
            lease_id=self._dependencies.lease_ids(),
            worker_id=worker_id,
            run_id=run_id,
            document_id=self._dependencies.document_ids(),
            virtual_document_namespace=f"AizimSmoke.Workers.W_{namespace_hash}",
            epoch_pair=epoch_pair,
            file_version=0,
            content_hash=content_hash,
            expires_at=self._dependencies.clock() + self._dependencies.lease_lifetime,
            physical_file=None,
            recovery_metadata=(("display_name", relative.as_posix()),),
        )

    def _compensate(self, preparation: DocumentPreparation | None, replaced: bool) -> None:
        if preparation is None:
            return
        if replaced:
            self._storage.restore(preparation.document)
        self._state.recover_document_edit(preparation)

    def _compensate_or_raise(self, preparation: DocumentPreparation | None, replaced: bool) -> None:
        try:
            self._compensate(preparation, replaced)
        except Exception:
            raise DocumentBrokerError("DOCUMENT_RECOVERY_FAILED") from None

    def _discard_or_raise(self, run_id: str, relative: PurePosixPath) -> None:
        try:
            self._storage.discard(run_id, relative)
        except Exception:
            raise DocumentBrokerError("DOCUMENT_RECOVERY_FAILED") from None

    @staticmethod
    def _snapshot(document: DocumentState, body: bytes) -> DocumentSnapshot:
        return DocumentSnapshot(
            document.document_id,
            document.relative_path,
            document.epoch_pair,
            document.file_version,
            document.content_hash,
            body,
        )


def _committed(document: DocumentState, content_hash: str) -> DocumentState:
    return DocumentState(
        document.document_id,
        document.run_id,
        document.worker_id,
        document.lease_id,
        document.relative_path,
        document.virtual_document_namespace,
        document.epoch_pair,
        document.file_version + 1,
        content_hash,
        document.expires_at,
    )
