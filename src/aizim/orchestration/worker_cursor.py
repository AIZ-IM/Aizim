from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from aizim.domain import EpochPair
from aizim.domain.serialization import JsonValue
from aizim.state.store_contracts import EventRecord


@dataclass(frozen=True, slots=True)
class WorkerCursor:
    directive_id: str
    worker_id: str
    execution_id: str
    last_event_sequence: int
    last_acknowledged_delta: str | None
    lease_id: str | None
    document_id: str | None
    document_version: int | None
    epoch_pair: EpochPair
    remaining_budget: int
    terminal: bool


def last_ack(records: tuple[EventRecord, ...], worker_id: str) -> str | None:
    last: str | None = None
    for record in records:
        event = record.envelope
        if (
            event.event_type == "KnowledgeDeltaAcknowledged"
            and event.payload.get("worker_id") == worker_id
        ):
            delta_id = event.payload.get("delta_id")
            if type(delta_id) is str:
                last = delta_id
    return last


def cursor_from_payload(payload: Mapping[str, JsonValue]) -> WorkerCursor:
    directive_id = _text(payload, "directive_id")
    worker_id = _text(payload, "worker_id")
    execution_id = _text(payload, "execution_id")
    base_epoch = _text(payload, "base_epoch")
    document_version = _optional_integer(payload, "document_version")
    terminal = payload.get("terminal")
    if type(terminal) is not bool:
        raise ValueError("INVALID_WORKER_CURSOR")
    return WorkerCursor(
        directive_id,
        worker_id,
        execution_id,
        _integer(payload, "last_event_sequence"),
        _optional_text(payload, "last_acknowledged_delta"),
        _optional_text(payload, "lease_id"),
        _optional_text(payload, "document_id"),
        document_version,
        EpochPair(base_epoch, _integer(payload, "knowledge_epoch")),
        _integer(payload, "remaining_budget"),
        terminal,
    )


def _text(payload: Mapping[str, JsonValue], field: str) -> str:
    value = payload.get(field)
    if type(value) is not str or not value:
        raise ValueError("INVALID_WORKER_CURSOR")
    return value


def _optional_text(payload: Mapping[str, JsonValue], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if type(value) is not str or not value:
        raise ValueError("INVALID_WORKER_CURSOR")
    return value


def _integer(payload: Mapping[str, JsonValue], field: str) -> int:
    value = payload.get(field)
    if type(value) is not int or value < 0:
        raise ValueError("INVALID_WORKER_CURSOR")
    return value


def _optional_integer(payload: Mapping[str, JsonValue], field: str) -> int | None:
    value = payload.get(field)
    return None if value is None else _integer(payload, field)
