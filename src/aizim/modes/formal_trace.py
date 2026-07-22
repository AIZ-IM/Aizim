from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final

from aizim.domain import canonical_json, sha256_bytes, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.state.events import event_as_dict
from aizim.state.store_contracts import EventRecord

_HASH = re.compile(r"[0-9a-f]{64}")
_ZERO_HASH: Final = "0" * 64
_EVENT_KINDS: Final[dict[str, tuple[str, ...]]] = {
    "RunCreated": ("run",),
    "ScheduleProposed": ("directive",),
    "WorkerRegistered": ("worker",),
    "WorkerStarted": ("worker",),
    "WorkerStopped": ("worker",),
    "WorkerCrashed": ("worker",),
    "LeaseGranted": ("lease",),
    "LeaseReleased": ("lease",),
    "DocumentEdited": ("accepted_document_action",),
    "ContributionSubmitted": ("contribution",),
    "PromotionVerificationRecorded": ("diagnostics", "build", "axiom_verification"),
    "PromotionPrepared": ("promotion",),
    "DeclarationPublished": ("publication",),
    "KnowledgeDeltaPublished": ("epoch", "delta"),
    "KnowledgeDeltaAcknowledged": ("delta_consumption",),
    "AlignmentReviewed": ("alignment_review",),
    "RunCompleted": ("terminal",),
    "RunAborted": ("terminal",),
}


@dataclass(frozen=True, slots=True)
class FormalTraceRecord:
    ordinal: int
    sequence: int
    kind: str
    source_event_id: str
    source_event_type: str
    payload_hash: str
    content_hashes: tuple[str, ...]
    previous_hash: str
    record_hash: str


def build_formal_trace(events: tuple[EventRecord, ...]) -> tuple[FormalTraceRecord, ...]:
    records: list[FormalTraceRecord] = []
    previous_hash = _ZERO_HASH
    for event_record in events:
        document = event_as_dict(event_record.envelope)
        payload = document["payload"]
        payload_hash = sha256_json(payload)
        hashes = _content_hashes(payload, payload_hash)
        for kind in _event_kinds(event_record.envelope.event_type, payload):
            record = _record(
                len(records),
                event_record.sequence,
                kind,
                event_record.envelope.event_id,
                event_record.envelope.event_type,
                payload_hash,
                hashes,
                previous_hash,
            )
            records.append(record)
            previous_hash = record.record_hash
    return tuple(records)


def _event_kinds(event_type: str, payload: dict[str, JsonValue]) -> tuple[str, ...]:
    if event_type != "FormalActionRecorded":
        return _EVENT_KINDS.get(event_type, ("event",))
    action_kind = payload.get("action_kind")
    if type(action_kind) is not str or action_kind not in {"diagnostics", "goal", "trial"}:
        raise ValueError("INVALID_FORMAL_TRACE_ACTION")
    return (action_kind,)


def formal_trace_bytes(records: tuple[FormalTraceRecord, ...]) -> bytes:
    if any(type(record) is not FormalTraceRecord for record in records):
        raise ValueError("INVALID_FORMAL_TRACE")
    return b"".join(canonical_json(record) + b"\n" for record in records)


def read_formal_trace(body: bytes) -> tuple[FormalTraceRecord, ...]:
    if type(body) is not bytes:
        raise ValueError("INVALID_FORMAL_TRACE")
    parsed: list[FormalTraceRecord] = []
    for line in body.splitlines():
        if not line:
            continue
        raw: JsonValue = json.loads(line)
        if type(raw) is not dict:
            raise ValueError("INVALID_FORMAL_TRACE")
        parsed.append(_parsed_record(raw))
    return tuple(parsed)


def replay_formal_trace(records: tuple[FormalTraceRecord, ...], expected_digest: str) -> bool:
    if _HASH.fullmatch(expected_digest) is None:
        return False
    previous_hash = _ZERO_HASH
    for ordinal, record in enumerate(records):
        if record.ordinal != ordinal or record.previous_hash != previous_hash:
            return False
        expected_record = _record(
            record.ordinal,
            record.sequence,
            record.kind,
            record.source_event_id,
            record.source_event_type,
            record.payload_hash,
            record.content_hashes,
            previous_hash,
        )
        if expected_record.record_hash != record.record_hash:
            return False
        previous_hash = record.record_hash
    return sha256_bytes(formal_trace_bytes(records)) == expected_digest


def _record(
    ordinal: int,
    sequence: int,
    kind: str,
    source_event_id: str,
    source_event_type: str,
    payload_hash: str,
    content_hashes: tuple[str, ...],
    previous_hash: str,
) -> FormalTraceRecord:
    unsigned: dict[str, JsonValue] = {
        "ordinal": ordinal,
        "sequence": sequence,
        "kind": kind,
        "source_event_id": source_event_id,
        "source_event_type": source_event_type,
        "payload_hash": payload_hash,
        "content_hashes": list(content_hashes),
        "previous_hash": previous_hash,
    }
    return FormalTraceRecord(
        ordinal,
        sequence,
        kind,
        source_event_id,
        source_event_type,
        payload_hash,
        content_hashes,
        previous_hash,
        sha256_json(unsigned),
    )


def _content_hashes(payload: dict[str, JsonValue], payload_hash: str) -> tuple[str, ...]:
    hashes = {payload_hash}
    for key, value in payload.items():
        if key.endswith("_hash") and type(value) is str and _HASH.fullmatch(value):
            hashes.add(value)
    return tuple(sorted(hashes))


def _parsed_record(raw: dict[str, JsonValue]) -> FormalTraceRecord:
    fields = {
        "ordinal",
        "sequence",
        "kind",
        "source_event_id",
        "source_event_type",
        "payload_hash",
        "content_hashes",
        "previous_hash",
        "record_hash",
    }
    if set(raw) != fields:
        raise ValueError("INVALID_FORMAL_TRACE")
    ordinal, sequence, hashes = raw["ordinal"], raw["sequence"], raw["content_hashes"]
    if type(ordinal) is not int or type(sequence) is not int or type(hashes) is not list:
        raise ValueError("INVALID_FORMAL_TRACE")
    kind = _required_text(raw, "kind")
    source_event_id = _required_text(raw, "source_event_id")
    source_event_type = _required_text(raw, "source_event_type")
    payload_hash = _required_text(raw, "payload_hash")
    previous_hash = _required_text(raw, "previous_hash")
    record_hash = _required_text(raw, "record_hash")
    content_hashes: list[str] = []
    for value in hashes:
        if type(value) is not str or _HASH.fullmatch(value) is None:
            raise ValueError("INVALID_FORMAL_TRACE")
        content_hashes.append(value)
    if not content_hashes:
        raise ValueError("INVALID_FORMAL_TRACE")
    return FormalTraceRecord(
        ordinal,
        sequence,
        kind,
        source_event_id,
        source_event_type,
        payload_hash,
        tuple(content_hashes),
        previous_hash,
        record_hash,
    )


def _required_text(raw: dict[str, JsonValue], field: str) -> str:
    value = raw[field]
    if type(value) is not str:
        raise ValueError("INVALID_FORMAL_TRACE")
    return value
