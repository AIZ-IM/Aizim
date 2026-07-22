from __future__ import annotations

from datetime import datetime
from typing import cast

from aizim.domain import EpochPair

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
    DocumentStateError,
    LeaseState,
    document_from_projection,
    epoch_from_projection,
    is_active_document_lease_projection,
    is_document_state_projection,
    lease_from_projection,
    lease_payload,
)
from .event_payload import thaw_payload
from .events import EventEnvelope
from .projections import ProjectionRecord, ProjectionReducer
from .store_contracts import EventRecord
from .store_mutations import (
    EventMutation,
    MutationConnection,
    apply_event_mutation,
    revoke_lease_capabilities,
)


def execute_document_operation[T](
    connection: MutationConnection,
    reducer: ProjectionReducer,
    operation: DocumentOperation[T],
) -> T:
    if isinstance(operation, AccessDocument):
        return cast(T, _access(_snapshots(connection), operation))
    if isinstance(operation, ActiveLeases):
        return cast(T, _active_leases(_snapshots(connection)))
    if isinstance(operation, PreparedDocuments):
        documents = tuple(
            document_from_projection(record)
            for record in _snapshots(connection)
            if record.projection_name == "documents" and is_document_state_projection(record)
        )
        return cast(T, tuple(item for item in documents if item.prepared_event_id))
    connection.execute("BEGIN IMMEDIATE")
    try:
        result = _mutate(connection, reducer, operation, _snapshots(connection))
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return cast(T, result)


def _mutate(
    connection: MutationConnection,
    reducer: ProjectionReducer,
    operation: DocumentOperation[object],
    snapshots: tuple[ProjectionRecord, ...],
) -> EventRecord | DocumentPreparation:
    if isinstance(operation, GrantLease):
        _validate_grant(snapshots, operation)
        result: EventRecord | DocumentPreparation = _append(
            connection, reducer, snapshots, operation.event
        )
    elif isinstance(operation, PrepareEdit):
        document = _access(snapshots, operation.access)
        document.validate_expected(operation.expected_version, operation.expected_hash)
        _event_matches(document, operation.event, document.file_version, document.content_hash)
        _append(connection, reducer, snapshots, operation.event)
        result = DocumentPreparation(operation.event.event_id, document)
    elif isinstance(operation, CommitEdit):
        document = _prepared(snapshots, operation.preparation_id)
        if operation.event.causation_id != operation.preparation_id:
            raise DocumentStateError("DOCUMENT_PREPARATION_MISMATCH")
        _validate_document_lease(snapshots, document, operation.now)
        _event_matches(document, operation.event, document.file_version + 1)
        result = _append(connection, reducer, snapshots, operation.event)
    elif isinstance(operation, RecoverEdit):
        document = _prepared(snapshots, operation.preparation_id)
        if operation.event.causation_id != operation.preparation_id:
            raise DocumentStateError("DOCUMENT_PREPARATION_MISMATCH")
        _event_matches(document, operation.event, document.file_version, document.content_hash)
        result = _append(connection, reducer, snapshots, operation.event)
    elif isinstance(operation, TerminateLease):
        lease = _lease(snapshots, operation.lease_id)
        if not lease.active:
            raise DocumentStateError("LEASE_INACTIVE")
        if lease.run_id != operation.run_id:
            raise DocumentStateError("LEASE_RUN_MISMATCH")
        if operation.worker_id is not None and lease.worker_id != operation.worker_id:
            raise DocumentStateError("LEASE_OWNER_MISMATCH")
        result = _append(connection, reducer, snapshots, operation.event)
    else:
        raise DocumentStateError("INVALID_DOCUMENT_OPERATION")
    revoke_lease_capabilities(connection, operation.event)
    return result


def _snapshots(connection: MutationConnection) -> tuple[ProjectionRecord, ...]:
    rows = connection.execute(
        "SELECT projection_name,entity_id,version,state_json FROM projections "
        "ORDER BY projection_name,entity_id"
    ).fetchall()
    records: list[ProjectionRecord] = []
    for row in rows:
        name, entity_id, version, state_json = row
        if not isinstance(name, str) or not isinstance(entity_id, str):
            raise DocumentStateError("INVALID_DOCUMENT_STATE")
        if type(version) is not int or not isinstance(state_json, str):
            raise DocumentStateError("INVALID_DOCUMENT_STATE")
        records.append(ProjectionRecord(name, entity_id, version, state_json.encode()))
    return tuple(records)


def _append(
    connection: MutationConnection,
    reducer: ProjectionReducer,
    snapshots: tuple[ProjectionRecord, ...],
    event: EventEnvelope,
) -> EventRecord:
    return apply_event_mutation(connection, EventMutation(reducer, event, snapshots))


def _epoch(snapshots: tuple[ProjectionRecord, ...]) -> EpochPair:
    record = next(
        (
            item
            for item in snapshots
            if item.projection_name == "epochs" and item.entity_id == "global"
        ),
        None,
    )
    if record is None:
        raise DocumentStateError("EPOCH_MISMATCH")
    return epoch_from_projection(record)


def _lease(snapshots: tuple[ProjectionRecord, ...], lease_id: str) -> LeaseState:
    record = next(
        (
            item
            for item in snapshots
            if item.projection_name == "leases" and item.entity_id == lease_id
        ),
        None,
    )
    if record is None:
        raise DocumentStateError("LEASE_NOT_FOUND")
    return lease_from_projection(record)


def _document(snapshots: tuple[ProjectionRecord, ...], document_id: str) -> DocumentState:
    record = next(
        (
            item
            for item in snapshots
            if item.projection_name == "documents" and item.entity_id == document_id
        ),
        None,
    )
    if record is None:
        raise DocumentStateError("DOCUMENT_NOT_FOUND")
    return document_from_projection(record)


def _access(snapshots: tuple[ProjectionRecord, ...], operation: AccessDocument) -> DocumentState:
    lease = _lease(snapshots, operation.lease_id)
    lease.validate_access(
        operation.run_id,
        operation.worker_id,
        operation.document_id,
        operation.now,
        _epoch(snapshots),
    )
    document = _document(snapshots, operation.document_id)
    if document.prepared_event_id is not None:
        raise DocumentStateError("DOCUMENT_EDIT_IN_PROGRESS")
    if document.lease_id != lease.lease_id:
        raise DocumentStateError("LEASE_DOCUMENT_MISMATCH")
    return document


def _active_leases(snapshots: tuple[ProjectionRecord, ...]) -> tuple[LeaseState, ...]:
    leases = (
        lease_from_projection(record)
        for record in snapshots
        if record.projection_name == "leases" and is_active_document_lease_projection(record)
    )
    return tuple(leases)


def _validate_grant(snapshots: tuple[ProjectionRecord, ...], operation: GrantLease) -> None:
    lease = operation.lease
    if lease.physical_file is not None:
        raise DocumentStateError("INVALID_DOCUMENT_STATE")
    if lease.epoch_pair != _epoch(snapshots):
        raise DocumentStateError("EPOCH_MISMATCH")
    if lease.expires_at <= operation.now:
        raise DocumentStateError("LEASE_EXPIRED")
    if operation.event.run_id != lease.run_id:
        raise DocumentStateError("LEASE_RUN_MISMATCH")
    if any(item.lease_id == lease.lease_id for item in _active_leases(snapshots)):
        raise DocumentStateError("LEASE_ID_COLLISION")
    if any(item.document_id == lease.document_id for item in _active_leases(snapshots)):
        raise DocumentStateError("DOCUMENT_ALREADY_LEASED")
    if any(
        item.relative_path.as_posix() == operation.relative_path
        for item in _active_leases(snapshots)
    ):
        raise DocumentStateError("DOCUMENT_ALREADY_LEASED")
    if any(
        item.projection_name == "documents" and item.entity_id == lease.document_id
        for item in snapshots
    ):
        raise DocumentStateError("DOCUMENT_ID_COLLISION")
    payload = thaw_payload(operation.event.payload)
    expected = lease_payload(lease, operation.relative_path)
    if payload != expected:
        raise DocumentStateError("INVALID_DOCUMENT_STATE")


def _prepared(snapshots: tuple[ProjectionRecord, ...], preparation_id: str) -> DocumentState:
    documents = (
        document_from_projection(record)
        for record in snapshots
        if record.projection_name == "documents" and is_document_state_projection(record)
    )
    prepared = next((item for item in documents if item.prepared_event_id == preparation_id), None)
    if prepared is None:
        raise DocumentStateError("DOCUMENT_PREPARATION_MISMATCH")
    return prepared


def _validate_document_lease(
    snapshots: tuple[ProjectionRecord, ...], document: DocumentState, now: datetime
) -> None:
    lease = _lease(snapshots, document.lease_id)
    lease.validate_access(
        document.run_id, document.worker_id, document.document_id, now, _epoch(snapshots)
    )


def _event_matches(
    document: DocumentState,
    event: EventEnvelope,
    version: int,
    content_hash: str | None = None,
) -> None:
    candidate = event.payload.get("content_hash") if content_hash is None else content_hash
    if type(candidate) is not str:
        raise DocumentStateError("INVALID_DOCUMENT_STATE")
    expected = document.payload()
    expected["version"] = version
    expected["content_hash"] = candidate
    if event.event_type == "DocumentEditPrepared":
        expected.update(expected_version=version, expected_hash=candidate)
    if event.run_id != document.run_id or thaw_payload(event.payload) != expected:
        raise DocumentStateError("INVALID_DOCUMENT_STATE")
