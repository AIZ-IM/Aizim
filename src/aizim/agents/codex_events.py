from __future__ import annotations

import json
import os
import re
import stat
from hashlib import sha256
from pathlib import Path
from typing import Final, Literal

from aizim.domain import canonical_json, sha256_json
from aizim.domain.serialization import JsonValue

_TOKEN_SHAPED: Final = re.compile(r"[A-Za-z0-9_-]{32,}")
_FINAL_LIMIT: Final = 16 * 1024


class CodexEventError(RuntimeError):
    pass


class TransportEventHasher:
    __slots__ = ("_digest",)

    def __init__(self) -> None:
        self._digest = sha256()

    def add(self, line: bytes) -> None:
        try:
            value: JsonValue = json.loads(line)
            if type(value) is not dict:
                raise ValueError
            encoded = canonical_json(_redact(value))
        except (ValueError, RecursionError) as error:
            raise CodexEventError("CODEX_JSONL_INVALID") from error
        self._digest.update(len(encoded).to_bytes(8, "big"))
        self._digest.update(encoded)

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def parse_final_message(
    path: Path,
) -> tuple[Literal["submitted", "abstained", "failed"], str, str]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise CodexEventError("CODEX_RESULT_INVALID") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > _FINAL_LIMIT
        ):
            raise CodexEventError("CODEX_RESULT_INVALID")
        raw = bytearray()
        while chunk := os.read(descriptor, _FINAL_LIMIT + 1 - len(raw)):
            raw.extend(chunk)
            if len(raw) > _FINAL_LIMIT:
                raise CodexEventError("CODEX_RESULT_INVALID")
    finally:
        os.close(descriptor)
    try:
        value: JsonValue = json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise CodexEventError("CODEX_RESULT_INVALID") from error
    if type(value) is not dict or value.keys() != {"status", "summary"}:
        raise CodexEventError("CODEX_RESULT_INVALID")
    status, summary = value["status"], value["summary"]
    if (
        type(status) is not str
        or status not in {"submitted", "abstained", "failed"}
        or type(summary) is not str
    ):
        raise CodexEventError("CODEX_RESULT_INVALID")
    if not 1 <= len(summary) <= 2000:
        raise CodexEventError("CODEX_RESULT_INVALID")
    safe_summary = _TOKEN_SHAPED.sub("<redacted>", summary)
    if status == "submitted":
        parsed_status = "submitted"
    elif status == "abstained":
        parsed_status = "abstained"
    else:
        parsed_status = "failed"
    document = {"status": parsed_status, "summary": safe_summary}
    return parsed_status, safe_summary, sha256_json(document)


def _redact(value: JsonValue) -> JsonValue:
    if type(value) is str:
        return _TOKEN_SHAPED.sub("<redacted>", value)
    if type(value) is list:
        return [_redact(item) for item in value]
    if type(value) is dict:
        return {_TOKEN_SHAPED.sub("<redacted>", key): _redact(item) for key, item in value.items()}
    return value
