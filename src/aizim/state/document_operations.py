from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aizim.domain import FileLease

from .documents import DocumentPreparation, DocumentState, LeaseState
from .events import EventEnvelope
from .store_contracts import EventRecord


class DocumentOperation[T]:
    pass


@dataclass(frozen=True, slots=True)
class GrantLease(DocumentOperation[EventRecord]):
    lease: FileLease
    relative_path: str
    now: datetime
    event: EventEnvelope


@dataclass(frozen=True, slots=True)
class AccessDocument(DocumentOperation[DocumentState]):
    run_id: str
    worker_id: str
    lease_id: str
    document_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class PrepareEdit(DocumentOperation[DocumentPreparation]):
    access: AccessDocument
    expected_version: int
    expected_hash: str
    event: EventEnvelope


@dataclass(frozen=True, slots=True)
class CommitEdit(DocumentOperation[EventRecord]):
    preparation_id: str
    now: datetime
    event: EventEnvelope


@dataclass(frozen=True, slots=True)
class RecoverEdit(DocumentOperation[EventRecord]):
    preparation_id: str
    event: EventEnvelope


@dataclass(frozen=True, slots=True)
class TerminateLease(DocumentOperation[EventRecord]):
    run_id: str
    worker_id: str | None
    lease_id: str
    event: EventEnvelope


@dataclass(frozen=True, slots=True)
class ActiveLeases(DocumentOperation[tuple[LeaseState, ...]]):
    pass


@dataclass(frozen=True, slots=True)
class PreparedDocuments(DocumentOperation[tuple[DocumentState, ...]]):
    pass
