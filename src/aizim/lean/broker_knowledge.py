from __future__ import annotations

import json

from aizim.domain import EpochPair
from aizim.state import StateService

from .models import DocumentBrokerError
from .project import PublishedModule


def current_epoch(state: StateService) -> EpochPair:
    record = state.query_projection("epochs", "global")
    if record is None:
        raise DocumentBrokerError("EPOCH_MISMATCH")
    try:
        payload = json.loads(record.state_json)
        if type(payload) is not dict:
            raise ValueError
        return EpochPair(payload["base_epoch"], payload["knowledge_epoch"])
    except (KeyError, TypeError, ValueError):
        raise DocumentBrokerError("EPOCH_MISMATCH") from None


def published_modules(state: StateService) -> tuple[PublishedModule, ...]:
    modules: list[PublishedModule] = []
    for record in state.query_events():
        event = record.envelope
        if event.event_type != "DeclarationPublished" or event.run_id is None:
            continue
        module, content_hash = event.payload.get("module"), event.payload.get("content_hash")
        if type(module) is str and type(content_hash) is str:
            modules.append(PublishedModule(event.run_id, module, content_hash))
    return tuple(modules)
