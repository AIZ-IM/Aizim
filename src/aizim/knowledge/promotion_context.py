from __future__ import annotations

import json
from collections.abc import Mapping

from aizim.domain import EpochPair
from aizim.state import EventEnvelope, StateService

from .errors import PromotionError


def find_contribution(state: StateService, contribution_id: str) -> EventEnvelope:
    for record in state.query_events():
        event = record.envelope
        if (
            event.event_type == "ContributionSubmitted"
            and event.payload.get("contribution_id") == contribution_id
        ):
            return event
    raise PromotionError("PROMOTION_NOT_FOUND")


def contribution_run_id(state: StateService, contribution_id: str) -> str | None:
    return find_contribution(state, contribution_id).run_id


def current_epoch(state: StateService) -> EpochPair:
    record = state.query_projection("epochs", "global")
    if record is None:
        raise PromotionError("EPOCH_MISMATCH")
    payload = _object_mapping(json.loads(record.state_json))
    if payload is None:
        raise PromotionError("EPOCH_MISMATCH")
    return EpochPair(text_field(payload, "base_epoch"), integer_field(payload, "knowledge_epoch"))


def is_stale(state: StateService, payload: Mapping[str, object], current: EpochPair) -> bool:
    if (
        text_field(payload, "base_epoch"),
        integer_field(payload, "knowledge_epoch"),
    ) != (current.base_epoch, current.knowledge_epoch):
        return True
    if text_field(payload, "payload_kind") != "patch":
        return False
    document = document_for(state, payload)
    return document is None or integer_field(document, "version") != integer_field(
        payload, "expected_file_version"
    )


def document_for(state: StateService, payload: Mapping[str, object]) -> dict[str, object] | None:
    record = state.query_projection("documents", text_field(payload, "document_id"))
    if record is None:
        return None
    state_payload = _object_mapping(json.loads(record.state_json))
    return None if state_payload is None else _object_mapping(state_payload.get("payload"))


def _object_mapping(value: object) -> dict[str, object] | None:
    if type(value) is not dict:
        return None
    result: dict[str, object] = {}
    for key, item in value.items():
        if type(key) is not str:
            return None
        result[key] = item
    return result


def text_field(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if type(value) is not str or not value:
        raise PromotionError("INVALID_CONTRIBUTION")
    return value


def integer_field(payload: Mapping[str, object], field: str) -> int:
    value = payload.get(field)
    if type(value) is not int or value < 0:
        raise PromotionError("INVALID_CONTRIBUTION")
    return value


def strings_field(payload: Mapping[str, object], field: str) -> tuple[str, ...]:
    value = payload.get(field)
    if type(value) is not list:
        raise PromotionError("INVALID_CONTRIBUTION")
    values: list[str] = []
    for item in value:
        if type(item) is not str or not item:
            raise PromotionError("INVALID_CONTRIBUTION")
        values.append(item)
    return tuple(values)
