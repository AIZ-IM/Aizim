from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from aizim.domain import EpochPair, FileLease
from aizim.domain.serialization import JsonValue

from .projections import ProjectionRecord

_DOCUMENT_STATE_EVENT_TYPES = frozenset(
    {"LeaseGranted", "DocumentEditPrepared", "DocumentEdited", "DocumentEditRecovered"}
)


@dataclass(slots=True)
class DocumentStateError(RuntimeError):
    code: str

    def __str__(self) -> str:
        return self.code


def canonical_document_timestamp(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise DocumentStateError("INVALID_DOCUMENT_STATE")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _timestamp(value: JsonValue) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise DocumentStateError("INVALID_DOCUMENT_STATE")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        raise DocumentStateError("INVALID_DOCUMENT_STATE") from None
    if canonical_document_timestamp(parsed) != value:
        raise DocumentStateError("INVALID_DOCUMENT_STATE")
    return parsed


def _text(payload: dict[str, JsonValue], name: str) -> str:
    value = payload.get(name)
    if type(value) is not str or not value:
        raise DocumentStateError("INVALID_DOCUMENT_STATE")
    return value


def _integer(payload: dict[str, JsonValue], name: str) -> int:
    value = payload.get(name)
    if type(value) is not int or value < 0:
        raise DocumentStateError("INVALID_DOCUMENT_STATE")
    return value


def _hash(payload: dict[str, JsonValue], name: str) -> str:
    value = _text(payload, name)
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise DocumentStateError("INVALID_DOCUMENT_STATE")
    return value


@dataclass(frozen=True, slots=True)
class LeaseState:
    lease_id: str
    worker_id: str
    run_id: str
    document_id: str
    relative_path: PurePosixPath
    virtual_document_namespace: str
    epoch_pair: EpochPair
    file_version: int
    content_hash: str
    expires_at: datetime
    active: bool

    def validate_access(
        self,
        run_id: str,
        worker_id: str,
        document_id: str,
        now: datetime,
        epoch_pair: EpochPair,
    ) -> None:
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise DocumentStateError("INVALID_DOCUMENT_TIME")
        if not self.active:
            raise DocumentStateError("LEASE_INACTIVE")
        if self.run_id != run_id:
            raise DocumentStateError("LEASE_RUN_MISMATCH")
        if self.worker_id != worker_id:
            raise DocumentStateError("LEASE_OWNER_MISMATCH")
        if self.document_id != document_id:
            raise DocumentStateError("LEASE_DOCUMENT_MISMATCH")
        if now >= self.expires_at:
            raise DocumentStateError("LEASE_EXPIRED")
        if self.epoch_pair != epoch_pair:
            raise DocumentStateError("EPOCH_MISMATCH")

    def payload(self) -> dict[str, JsonValue]:
        return {
            "lease_id": self.lease_id,
            "worker_id": self.worker_id,
            "document_id": self.document_id,
            "relative_path": self.relative_path.as_posix(),
            "virtual_document_namespace": self.virtual_document_namespace,
            "base_epoch": self.epoch_pair.base_epoch,
            "knowledge_epoch": self.epoch_pair.knowledge_epoch,
            "version": self.file_version,
            "content_hash": self.content_hash,
            "expires_at": canonical_document_timestamp(self.expires_at),
        }


@dataclass(frozen=True, slots=True)
class DocumentState:
    document_id: str
    run_id: str
    worker_id: str
    lease_id: str
    relative_path: PurePosixPath
    virtual_document_namespace: str
    epoch_pair: EpochPair
    file_version: int
    content_hash: str
    expires_at: datetime
    prepared_event_id: str | None = None

    def validate_expected(self, version: int, content_hash: str) -> None:
        if type(version) is not int or self.file_version != version:
            raise DocumentStateError("DOCUMENT_VERSION_MISMATCH")
        if type(content_hash) is not str or self.content_hash != content_hash:
            raise DocumentStateError("DOCUMENT_HASH_MISMATCH")

    def payload(self) -> dict[str, JsonValue]:
        return LeaseState(
            self.lease_id,
            self.worker_id,
            self.run_id,
            self.document_id,
            self.relative_path,
            self.virtual_document_namespace,
            self.epoch_pair,
            self.file_version,
            self.content_hash,
            self.expires_at,
            True,
        ).payload()


@dataclass(frozen=True, slots=True)
class DocumentPreparation:
    preparation_id: str
    document: DocumentState


def projection_payload(record: ProjectionRecord) -> tuple[str, str | None, dict[str, JsonValue]]:
    raw: JsonValue = json.loads(record.state_json)
    if type(raw) is not dict:
        raise DocumentStateError("INVALID_DOCUMENT_STATE")
    event_type = raw.get("event_type")
    run_id = raw.get("run_id")
    payload = raw.get("payload")
    if type(event_type) is not str or (run_id is not None and type(run_id) is not str):
        raise DocumentStateError("INVALID_DOCUMENT_STATE")
    if type(payload) is not dict:
        raise DocumentStateError("INVALID_DOCUMENT_STATE")
    return event_type, run_id, payload


def is_document_state_projection(record: ProjectionRecord) -> bool:
    event_type, _, _ = projection_payload(record)
    return event_type in _DOCUMENT_STATE_EVENT_TYPES


def is_active_document_lease_projection(record: ProjectionRecord) -> bool:
    event_type, _, _ = projection_payload(record)
    return event_type == "LeaseGranted"


def lease_from_projection(record: ProjectionRecord) -> LeaseState:
    event_type, run_id, payload = projection_payload(record)
    if run_id is None:
        raise DocumentStateError("INVALID_DOCUMENT_STATE")
    return LeaseState(
        lease_id=_text(payload, "lease_id"),
        worker_id=_text(payload, "worker_id"),
        run_id=run_id,
        document_id=_text(payload, "document_id"),
        relative_path=PurePosixPath(_text(payload, "relative_path")),
        virtual_document_namespace=_text(payload, "virtual_document_namespace"),
        epoch_pair=EpochPair(_hash(payload, "base_epoch"), _integer(payload, "knowledge_epoch")),
        file_version=_integer(payload, "version"),
        content_hash=_hash(payload, "content_hash"),
        expires_at=_timestamp(payload.get("expires_at")),
        active=event_type == "LeaseGranted",
    )


def document_from_projection(record: ProjectionRecord) -> DocumentState:
    event_type, run_id, payload = projection_payload(record)
    if run_id is None:
        raise DocumentStateError("INVALID_DOCUMENT_STATE")
    raw: JsonValue = json.loads(record.state_json)
    if type(raw) is not dict:
        raise DocumentStateError("INVALID_DOCUMENT_STATE")
    event_id = raw.get("event_id")
    if type(event_id) is not str:
        raise DocumentStateError("INVALID_DOCUMENT_STATE")
    return DocumentState(
        document_id=_text(payload, "document_id"),
        run_id=run_id,
        worker_id=_text(payload, "worker_id"),
        lease_id=_text(payload, "lease_id"),
        relative_path=PurePosixPath(_text(payload, "relative_path")),
        virtual_document_namespace=_text(payload, "virtual_document_namespace"),
        epoch_pair=EpochPair(_hash(payload, "base_epoch"), _integer(payload, "knowledge_epoch")),
        file_version=_integer(payload, "version"),
        content_hash=_hash(payload, "content_hash"),
        expires_at=_timestamp(payload.get("expires_at")),
        prepared_event_id=event_id if event_type == "DocumentEditPrepared" else None,
    )


def epoch_from_projection(record: ProjectionRecord) -> EpochPair:
    raw: JsonValue = json.loads(record.state_json)
    if type(raw) is not dict:
        raise DocumentStateError("EPOCH_MISMATCH")
    base_epoch = raw.get("base_epoch")
    knowledge_epoch = raw.get("knowledge_epoch")
    if type(base_epoch) is not str or type(knowledge_epoch) is not int:
        raise DocumentStateError("EPOCH_MISMATCH")
    try:
        return EpochPair(base_epoch, knowledge_epoch)
    except ValueError:
        raise DocumentStateError("EPOCH_MISMATCH") from None


def lease_payload(lease: FileLease, relative_path: str) -> dict[str, JsonValue]:
    return LeaseState(
        lease.lease_id,
        lease.worker_id,
        lease.run_id,
        lease.document_id,
        PurePosixPath(relative_path),
        lease.virtual_document_namespace,
        lease.epoch_pair,
        lease.file_version,
        lease.content_hash,
        lease.expires_at,
        True,
    ).payload()
