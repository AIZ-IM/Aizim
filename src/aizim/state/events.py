from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, TypedDict

from aizim.domain.serialization import JsonValue

from .event_payload import FrozenJsonObject, freeze_payload, thaw_payload
from .schema_v1 import EventValidationError, validate_payload

EVENT_SCHEMA_VERSION: Final = 1
_EVENT_FIELDS: Final = frozenset(
    {
        "event_id",
        "schema_version",
        "event_type",
        "occurred_at",
        "actor",
        "run_id",
        "causation_id",
        "payload",
    }
)
_ULID_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


@dataclass(frozen=True, slots=True)
class IncompatibleEventSchemaError(RuntimeError):
    source_version: int

    def __str__(self) -> str:
        return f"INCOMPATIBLE_EVENT_SCHEMA: unsupported event schema {self.source_version}"


@dataclass(frozen=True, slots=True, init=False)
class EventEnvelope:
    event_id: str
    schema_version: int
    event_type: str
    occurred_at: datetime
    actor: str
    run_id: str | None
    causation_id: str | None
    payload: FrozenJsonObject

    def __init__(
        self,
        event_id: str,
        schema_version: int,
        event_type: str,
        occurred_at: datetime,
        actor: str,
        run_id: str | None,
        causation_id: str | None,
        payload: dict[str, JsonValue],
    ) -> None:
        _text(event_id, "event_id")
        if len(event_id) != 26 or any(char not in _ULID_ALPHABET for char in event_id):
            raise EventValidationError("event_id", "must be a 26-character Crockford ULID")
        if schema_version != EVENT_SCHEMA_VERSION:
            raise IncompatibleEventSchemaError(schema_version)
        _text(event_type, "event_type")
        _text(actor, "actor")
        if occurred_at.tzinfo is None or occurred_at.utcoffset() != timedelta(0):
            raise EventValidationError("occurred_at", "must be timezone-aware UTC")
        for location, value in (("run_id", run_id), ("causation_id", causation_id)):
            if value is not None:
                _text(value, location)
        if type(payload) is not dict:
            raise EventValidationError("payload", "must be a JSON object")
        validate_payload(event_type, payload)
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "causation_id", causation_id)
        object.__setattr__(self, "payload", freeze_payload(payload))


class EventDocument(TypedDict):
    event_id: str
    schema_version: int
    event_type: str
    occurred_at: str
    actor: str
    run_id: str | None
    causation_id: str | None
    payload: dict[str, JsonValue]


def _text(value: str, location: str) -> None:
    if type(value) is not str or not value:
        raise EventValidationError(location, "must be a non-empty string")


def _timestamp(value: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise EventValidationError("occurred_at", "must be canonical UTC RFC 3339")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise EventValidationError("occurred_at", "must be canonical UTC RFC 3339") from error
    if parsed.isoformat().replace("+00:00", "Z") != value:
        raise EventValidationError("occurred_at", "must be canonical UTC RFC 3339")
    return parsed


def _required_text_value(event_dict: Mapping[str, JsonValue], key: str) -> str:
    value = event_dict[key]
    if type(value) is not str or not value:
        raise EventValidationError(key, "must be a non-empty string")
    return value


def _optional_text_value(event_dict: Mapping[str, JsonValue], key: str) -> str | None:
    value = event_dict[key]
    if value is None:
        return None
    if type(value) is not str or not value:
        raise EventValidationError(key, "must be a non-empty string or null")
    return value


def event_as_dict(event: EventEnvelope) -> EventDocument:
    return EventDocument(
        event_id=event.event_id,
        schema_version=event.schema_version,
        event_type=event.event_type,
        occurred_at=event.occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        actor=event.actor,
        run_id=event.run_id,
        causation_id=event.causation_id,
        payload=thaw_payload(event.payload),
    )


def _upcast_v1(event_dict: Mapping[str, JsonValue]) -> EventEnvelope:
    unknown = event_dict.keys() - _EVENT_FIELDS
    missing = _EVENT_FIELDS - event_dict.keys()
    if missing or unknown:
        details = missing if missing else unknown
        kind = "missing" if missing else "unknown"
        raise EventValidationError("event", f"{kind} fields: {', '.join(sorted(details))}")
    payload = event_dict["payload"]
    if type(payload) is not dict:
        raise EventValidationError("payload", "must be a JSON object")
    return EventEnvelope(
        event_id=_required_text_value(event_dict, "event_id"),
        schema_version=EVENT_SCHEMA_VERSION,
        event_type=_required_text_value(event_dict, "event_type"),
        occurred_at=_timestamp(_required_text_value(event_dict, "occurred_at")),
        actor=_required_text_value(event_dict, "actor"),
        run_id=_optional_text_value(event_dict, "run_id"),
        causation_id=_optional_text_value(event_dict, "causation_id"),
        payload=payload,
    )


_UPCASTERS: Final[dict[int, Callable[[Mapping[str, JsonValue]], EventEnvelope]]] = {
    1: _upcast_v1
}


def upcast(event_dict: Mapping[str, JsonValue]) -> EventEnvelope:
    source_version = event_dict.get("schema_version")
    if type(source_version) is not int:
        raise EventValidationError("schema_version", "must be an integer")
    upcaster = _UPCASTERS.get(source_version)
    if upcaster is None:
        raise IncompatibleEventSchemaError(source_version)
    return upcaster(event_dict)


class MonotoneUlidFactory:
    def __init__(self) -> None:
        self._last_milliseconds = -1
        self._randomness = 0

    def __call__(self) -> str:
        milliseconds = time.time_ns() // 1_000_000
        if milliseconds > self._last_milliseconds:
            self._last_milliseconds = milliseconds
            self._randomness = secrets.randbits(80)
        else:
            self._randomness += 1
            if self._randomness >= 1 << 80:
                self._last_milliseconds += 1
                self._randomness = 0
        value = (self._last_milliseconds << 80) | self._randomness
        characters = ["0"] * 26
        for index in range(25, -1, -1):
            characters[index] = _ULID_ALPHABET[value & 31]
            value >>= 5
        return "".join(characters)


def utc_now() -> datetime:
    return datetime.now(UTC)
