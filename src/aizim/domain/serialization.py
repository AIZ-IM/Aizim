from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import singledispatch
from hashlib import sha256
from pathlib import Path, PurePath

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class CanonicalSerializationError(ValueError):
    value_type: str
    reason: str

    def __str__(self) -> str:
        return f"cannot canonically serialize {self.value_type}: {self.reason}"


def _failure[T](value: T, reason: str) -> CanonicalSerializationError:
    return CanonicalSerializationError(type(value).__name__, reason)


def _sort_key(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


@singledispatch
def _canonicalize[T](value: T) -> JsonValue:
    if is_dataclass(value):
        return {
            field.name: _canonicalize(getattr(value, field.name))
            for field in fields(value)
        }
    raise _failure(value, "unsupported value")


@_canonicalize.register
def _canonicalize_enum(value: StrEnum) -> JsonValue:
    return value.value


@_canonicalize.register
def _canonicalize_datetime(value: datetime) -> JsonValue:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise _failure(value, "timestamp must be timezone-aware UTC")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@_canonicalize.register
def _canonicalize_bool(value: bool) -> JsonValue:
    return value


@_canonicalize.register
def _canonicalize_int(value: int) -> JsonValue:
    return value


@_canonicalize.register
def _canonicalize_float(value: float) -> JsonValue:
    if not math.isfinite(value):
        raise _failure(value, "floating-point value must be finite")
    return value


@_canonicalize.register
def _canonicalize_str(value: str) -> JsonValue:
    return value


@_canonicalize.register(type(None))
def _canonicalize_none(value: None) -> JsonValue:
    return value


@_canonicalize.register
def _canonicalize_path(value: PurePath) -> JsonValue:
    return value.as_posix()


@_canonicalize.register(dict)
def _canonicalize_dict[K, V](value: dict[K, V]) -> JsonValue:
    result: dict[str, JsonValue] = {}
    for key, item in value.items():
        if type(key) is not str:
            raise _failure(key, "dictionary keys must be strings")
        result[key] = _canonicalize(item)
    return result


@_canonicalize.register(tuple)
def _canonicalize_tuple[T](value: tuple[T, ...]) -> JsonValue:
    return [_canonicalize(item) for item in value]


@_canonicalize.register(list)
def _canonicalize_list[T](value: list[T]) -> JsonValue:
    return [_canonicalize(item) for item in value]


@_canonicalize.register(set)
def _canonicalize_set[T](value: set[T]) -> JsonValue:
    return sorted((_canonicalize(item) for item in value), key=_sort_key)


@_canonicalize.register(frozenset)
def _canonicalize_frozenset[T](value: frozenset[T]) -> JsonValue:
    return sorted((_canonicalize(item) for item in value), key=_sort_key)


@_canonicalize.register(type)
def _canonicalize_class[T](value: type[T]) -> JsonValue:
    raise _failure(value, "classes are not values")


def canonical_json[T](value: T) -> bytes:
    return json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_json[T](value: T) -> str:
    return sha256_bytes(canonical_json(value))


def _framed_hash(parts: Iterable[bytes]) -> str:
    digest = sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def compute_environment_fingerprint[T](
    project_root: Path,
    import_policy_manifest: Mapping[str, str],
    elaboration_config: T,
) -> str:
    parts = [
        (project_root / "lean-toolchain").read_bytes(),
        (project_root / "lakefile.toml").read_bytes(),
    ]
    lake_manifest = project_root / "lake-manifest.json"
    if lake_manifest.is_file():
        parts.append(lake_manifest.read_bytes())
    parts.extend(
        (
            canonical_json(tuple(sorted(import_policy_manifest.items()))),
            canonical_json(elaboration_config),
        )
    )
    return _framed_hash(parts)


def compute_base_epoch(
    environment_fingerprint: str,
    promoted_modules: Mapping[str, str],
) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", environment_fingerprint) is None:
        raise CanonicalSerializationError("environment_fingerprint", "expected lowercase hex")
    for content_hash in promoted_modules.values():
        if re.fullmatch(r"[0-9a-f]{64}", content_hash) is None:
            raise CanonicalSerializationError("module content hash", "expected lowercase hex")
    names = tuple(sorted(promoted_modules))
    hashes = tuple(promoted_modules[name].encode() for name in names)
    return _framed_hash((environment_fingerprint.encode(), canonical_json(names), *hashes))
