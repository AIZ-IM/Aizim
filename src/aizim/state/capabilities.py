from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aizim.domain.serialization import JsonValue, canonical_json


@dataclass(frozen=True, slots=True)
class CapabilityRecordError(ValueError):
    field: str
    reason: str

    def __str__(self) -> str:
        return f"{self.field}: {self.reason}"


def _timestamp(value: datetime, field: str) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise CapabilityRecordError(field, "must be a timezone-aware UTC datetime")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str, field: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise CapabilityRecordError(field, "must be canonical UTC RFC 3339")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise CapabilityRecordError(field, "must be canonical UTC RFC 3339") from error
    if _timestamp(parsed, field) != value:
        raise CapabilityRecordError(field, "must be canonical UTC RFC 3339")
    return parsed


@dataclass(frozen=True, slots=True, repr=False)
class CapabilityRecord:
    token_hash: str
    run_id: str
    worker_id: str
    role: str
    lease_id: str | None
    operations: tuple[str, ...]
    expires_at: datetime
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", self.token_hash) is None:
            raise CapabilityRecordError("token_hash", "must be a lowercase SHA-256 hash")
        fields = (("run_id", self.run_id), ("worker_id", self.worker_id), ("role", self.role))
        for field, value in fields:
            if type(value) is not str or not value:
                raise CapabilityRecordError(field, "must be a non-empty string")
        if self.lease_id is not None and (type(self.lease_id) is not str or not self.lease_id):
            raise CapabilityRecordError("lease_id", "must be a non-empty string when present")
        if (
            type(self.operations) is not tuple
            or not self.operations
            or any(type(operation) is not str or not operation for operation in self.operations)
            or len(self.operations) != len(set(self.operations))
        ):
            raise CapabilityRecordError("operations", "must be unique non-empty strings")
        _timestamp(self.expires_at, "expires_at")
        if self.revoked_at is not None:
            _timestamp(self.revoked_at, "revoked_at")

    def __repr__(self) -> str:
        return (
            "CapabilityRecord(token_hash=<redacted>, "
            f"run_id={self.run_id!r}, worker_id={self.worker_id!r}, role={self.role!r})"
        )


type CapabilityRow = tuple[str, str, str, str, str | None, str, str, str | None]


def capability_row(record: CapabilityRecord) -> CapabilityRow:
    return (
        record.token_hash,
        record.run_id,
        record.worker_id,
        record.role,
        record.lease_id,
        canonical_json(record.operations).decode(),
        _timestamp(record.expires_at, "expires_at"),
        None if record.revoked_at is None else _timestamp(record.revoked_at, "revoked_at"),
    )


def capability_from_row(row: CapabilityRow) -> CapabilityRecord:
    token_hash, run_id, worker_id, role, lease_id, raw_operations, expires_at, revoked_at = row
    operations: JsonValue = json.loads(raw_operations)
    if type(operations) is not list:
        raise CapabilityRecordError("operations", "stored value must be a JSON string array")
    parsed_operations: list[str] = []
    for operation in operations:
        if type(operation) is not str:
            raise CapabilityRecordError("operations", "stored value must be a JSON string array")
        parsed_operations.append(operation)
    return CapabilityRecord(
        token_hash=token_hash,
        run_id=run_id,
        worker_id=worker_id,
        role=role,
        lease_id=lease_id,
        operations=tuple(parsed_operations),
        expires_at=_parse_timestamp(expires_at, "expires_at"),
        revoked_at=None if revoked_at is None else _parse_timestamp(revoked_at, "revoked_at"),
    )


def canonical_timestamp(value: datetime) -> str:
    return _timestamp(value, "timestamp")
