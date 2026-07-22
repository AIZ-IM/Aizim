from __future__ import annotations

from typing import Final

import pytest

from aizim.domain.serialization import JsonValue
from aizim.state import EventValidationError
from aizim.state.schema_v1 import validate_payload

_HASH: Final = "a" * 64
_TIMESTAMP: Final = "2026-07-21T10:00:00Z"
_FORMAL_ACTION: Final[dict[str, JsonValue]] = {
    "action_id": "a",
    "worker_id": "w",
    "document_id": "d",
    "input_hash": _HASH,
    "output_hash": _HASH,
    "verdict": "success",
    "document_version": 0,
    "base_epoch": _HASH,
    "knowledge_epoch": 0,
    "started_at": _TIMESTAMP,
    "completed_at": _TIMESTAMP,
}


def _without(field: str) -> dict[str, JsonValue]:
    return {key: value for key, value in _FORMAL_ACTION.items() if key != field}


def _with(field: str, value: JsonValue) -> dict[str, JsonValue]:
    return {**_FORMAL_ACTION, field: value}


def test_formal_action_requires_complete_audit_metadata() -> None:
    validate_payload("FormalActionRecorded", _FORMAL_ACTION)

    for field in _FORMAL_ACTION:
        with pytest.raises(EventValidationError):
            validate_payload("FormalActionRecorded", _without(field))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("input_hash", "A" * 64),
        ("output_hash", "not-a-hash"),
        ("base_epoch", "z" * 64),
        ("document_version", -1),
        ("knowledge_epoch", -1),
        ("started_at", "2026-07-21T10:00:00+00:00"),
        ("completed_at", "2026-07-21T10:00:00+00:00"),
    ),
)
def test_formal_action_rejects_invalid_audit_metadata(field: str, value: JsonValue) -> None:
    with pytest.raises(EventValidationError):
        validate_payload("FormalActionRecorded", _with(field, value))
