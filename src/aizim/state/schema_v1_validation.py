from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Final

from aizim.domain.serialization import JsonValue


def text_payload(value: JsonValue) -> bool:
    return type(value) is str and bool(value)


def integer_payload(value: JsonValue) -> bool:
    return type(value) is int and value >= 0


def sha256_payload(value: JsonValue) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def timestamp_payload(value: JsonValue) -> bool:
    if type(value) is not str or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return parsed.isoformat().replace("+00:00", "Z") == value


def strings_payload(value: JsonValue) -> bool:
    return type(value) is list and all(type(item) is str and bool(item) for item in value)


def fields(value: str) -> tuple[str, ...]:
    return tuple(value.split())


DOCUMENT_FIELDS: Final = fields(
    "document_id lease_id worker_id relative_path virtual_document_namespace "
    "base_epoch knowledge_epoch version content_hash expires_at"
)
DOCUMENT_VALIDATORS: Final[dict[str, Callable[[JsonValue], bool]]] = {
    "base_epoch": sha256_payload,
    "content_hash": sha256_payload,
    "expires_at": timestamp_payload,
}
