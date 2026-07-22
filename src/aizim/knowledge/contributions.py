from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from aizim.domain import sha256_bytes

_FORBIDDEN: Final = ("sorry", "admit", "axiom", "unsafe", "lean_run_code")
_EXTRA_DECLARATIONS: Final = (
    "def",
    "abbrev",
    "opaque",
    "inductive",
    "structure",
    "class",
    "instance",
    "macro",
    "syntax",
    "elab",
)
_COMMANDS: Final = (
    "#check",
    "#eval",
    "#print",
    "attribute",
    "example",
    "export",
    "initialize",
    "local",
    "open",
    "run_tac",
    "set_option",
    "variable",
)


class ContributionValidationError(ValueError):
    pass


def _text(value: object, code: str) -> str:
    if type(value) is not str or not value:
        raise ContributionValidationError(code)
    return value


def _hash(value: object, code: str) -> str:
    value = _text(value, code)
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ContributionValidationError(code)
    return value


def _utf8(value: bytes) -> str:
    if type(value) is not bytes:
        raise ContributionValidationError("INVALID_UTF8")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        raise ContributionValidationError("INVALID_UTF8") from None


@dataclass(frozen=True, slots=True)
class ByteEdit:
    start_byte: int
    end_byte: int
    replacement_utf8: bytes

    def __post_init__(self) -> None:
        if (
            type(self.start_byte) is not int
            or type(self.end_byte) is not int
            or self.start_byte < 0
            or self.end_byte < self.start_byte
            or type(self.replacement_utf8) is not bytes
        ):
            raise ContributionValidationError("INVALID_BYTE_EDIT")
        _utf8(self.replacement_utf8)


@dataclass(frozen=True, slots=True)
class PatchPayload:
    expected_file_version: int
    expected_content_hash: str
    edits: tuple[ByteEdit, ...]

    def __post_init__(self) -> None:
        if type(self.expected_file_version) is not int or self.expected_file_version < 0:
            raise ContributionValidationError("INVALID_FILE_VERSION")
        _hash(self.expected_content_hash, "INVALID_CONTENT_HASH")
        if type(self.edits) is not tuple or not self.edits:
            raise ContributionValidationError("INVALID_BYTE_EDITS")
        if any(type(edit) is not ByteEdit for edit in self.edits):
            raise ContributionValidationError("INVALID_BYTE_EDITS")

    def apply(self, source: bytes) -> bytes:
        _utf8(source)
        if sha256_bytes(source) != self.expected_content_hash:
            raise ContributionValidationError("CONTENT_HASH_MISMATCH")
        cursor = 0
        output: list[bytes] = []
        for edit in self.edits:
            if (
                edit.start_byte < cursor
                or edit.end_byte > len(source)
                or not _boundary(source, edit.start_byte)
                or not _boundary(source, edit.end_byte)
            ):
                raise ContributionValidationError("INVALID_BYTE_EDITS")
            output.extend((source[cursor : edit.start_byte], edit.replacement_utf8))
            cursor = edit.end_byte
        output.append(source[cursor:])
        result = b"".join(output)
        _utf8(result)
        return result


@dataclass(frozen=True, slots=True)
class SnapshotPayload:
    source: bytes
    payload_hash: str

    def __post_init__(self) -> None:
        _utf8(self.source)
        if sha256_bytes(self.source) != _hash(self.payload_hash, "INVALID_PAYLOAD_HASH"):
            raise ContributionValidationError("PAYLOAD_HASH_MISMATCH")


def validate_candidate_source(
    source: bytes, candidate_name: str, complete_type: str, allowed_imports: tuple[str, ...]
) -> None:
    text = _utf8(source)
    name, candidate_type = (
        _text(candidate_name, "INVALID_CANDIDATE"),
        _text(complete_type, "INVALID_CANDIDATE"),
    )
    if type(allowed_imports) is not tuple or any(type(item) is not str for item in allowed_imports):
        raise ContributionValidationError("INVALID_IMPORT_POLICY")
    code = _strip_comments_and_strings(text)
    if any(re.search(rf"\b{token}\b", code) for token in _FORBIDDEN):
        raise ContributionValidationError("FORBIDDEN_LEAN_TOKEN")
    if len(re.findall(r"\btheorem\b", code)) != 1 or any(
        re.search(rf"\b{token}\b", code) for token in _EXTRA_DECLARATIONS
    ):
        raise ContributionValidationError("EXTRA_TOP_LEVEL_DECLARATION")
    if any(re.search(rf"(?m)^\s*{re.escape(command)}\b", code) for command in _COMMANDS):
        raise ContributionValidationError("FORBIDDEN_LEAN_COMMAND")
    imports = re.findall(r"(?m)^\s*import\s+([A-Za-z0-9_.]+)", code)
    if any(item not in allowed_imports for item in imports):
        raise ContributionValidationError("IMPORT_NOT_ALLOWED")
    normalized = re.sub(r"\s+", " ", code)
    separator = "" if candidate_type.startswith(("(", "{", "[")) else ": "
    declaration = re.escape(f"theorem {name} {separator}{candidate_type}")
    if re.search(rf"\b{declaration}(?=\s*:=)", normalized) is None:
        raise ContributionValidationError("CANDIDATE_NOT_IN_SOURCE")


def _boundary(source: bytes, offset: int) -> bool:
    try:
        source[:offset].decode("utf-8")
        source[offset:].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _strip_comments_and_strings(source: str) -> str:
    result: list[str] = []
    index = 0
    in_string = False
    block_depth = 0
    while index < len(source):
        pair = source[index : index + 2]
        if block_depth:
            if pair == "/-":
                block_depth += 1
                index += 2
            elif pair == "-/":
                block_depth -= 1
                index += 2
            else:
                index += 1
        elif in_string:
            if source[index] == "\\":
                index += 2
            elif source[index] == '"':
                in_string = False
                index += 1
            else:
                index += 1
        elif pair == "--":
            newline = source.find("\n", index)
            index = len(source) if newline < 0 else newline
        elif pair == "/-":
            block_depth, index = 1, index + 2
        elif source[index] == '"':
            in_string, index = True, index + 1
        else:
            result.append(source[index])
            index += 1
    return "".join(result)
