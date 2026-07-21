from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from typing_extensions import TypeIs

from aizim.domain.serialization import JsonValue

type FrozenJsonValue = (
    None
    | bool
    | int
    | float
    | str
    | tuple[FrozenJsonValue, ...]
    | Mapping[str, FrozenJsonValue]
)
type FrozenJsonObject = Mapping[str, FrozenJsonValue]


def _is_json_object(value: JsonValue) -> TypeIs[dict[str, JsonValue]]:
    return type(value) is dict


def _is_json_array(value: JsonValue) -> TypeIs[list[JsonValue]]:
    return type(value) is list


def _is_frozen_object(
    value: FrozenJsonValue,
) -> TypeIs[Mapping[str, FrozenJsonValue]]:
    return isinstance(value, Mapping)


def _is_frozen_array(
    value: FrozenJsonValue,
) -> TypeIs[tuple[FrozenJsonValue, ...]]:
    return type(value) is tuple


def _freeze(value: JsonValue) -> FrozenJsonValue:
    if _is_json_object(value):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if _is_json_array(value):
        return tuple(_freeze(item) for item in value)
    return value


def freeze_payload(payload: dict[str, JsonValue]) -> FrozenJsonObject:
    return MappingProxyType({key: _freeze(value) for key, value in payload.items()})


def _thaw(value: FrozenJsonValue) -> JsonValue:
    if _is_frozen_object(value):
        return {key: _thaw(item) for key, item in value.items()}
    if _is_frozen_array(value):
        return [_thaw(item) for item in value]
    return value


def thaw_payload(payload: FrozenJsonObject) -> dict[str, JsonValue]:
    return {key: _thaw(value) for key, value in payload.items()}
