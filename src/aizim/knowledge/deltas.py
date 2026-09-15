from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from aizim.domain import EpochPair, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.state import AppendEventCommand, StateService

from .artifacts import ArtifactReference


@dataclass(frozen=True, slots=True)
class PublicationDelta:
    new_epoch: EpochPair
    prepared: AppendEventCommand
    declaration: AppendEventCommand
    delta: AppendEventCommand


@dataclass(frozen=True, slots=True)
class PublishedKnowledgeDelta:
    delta_id: str
    previous_epoch: EpochPair
    new_epoch: EpochPair
    declaration_id: str
    fully_qualified_name: str
    complete_type: str
    module: str
    dependencies: tuple[str, ...]
    assumptions: tuple[str, ...]
    axioms: tuple[str, ...]
    evidence_links: tuple[str, ...]
    contribution_id: str
    publication_sequence: int


class KnowledgeReader:
    def __init__(self, state: StateService) -> None:
        if type(state) is not StateService:
            raise ValueError("INVALID_KNOWLEDGE_READER")
        self._state = state

    def read(self, after_knowledge_epoch: int) -> tuple[PublishedKnowledgeDelta, ...]:
        if type(after_knowledge_epoch) is not int or after_knowledge_epoch < 0:
            raise ValueError("INVALID_KNOWLEDGE_CURSOR")
        deltas = (
            _delta(record.envelope.payload)
            for record in self._state.query_events()
            if record.envelope.event_type == "KnowledgeDeltaPublished"
        )
        return tuple(
            item for item in deltas if item.new_epoch.knowledge_epoch > after_knowledge_epoch
        )

    def acknowledge(self, run_id: str, worker_id: str, delta_id: str) -> bool:
        if any(type(value) is not str or not value for value in (run_id, worker_id, delta_id)):
            raise ValueError("INVALID_KNOWLEDGE_ACKNOWLEDGEMENT")
        events = self._state.query_events(run_id)
        if not any(
            event.envelope.event_type == "KnowledgeDeltaPublished"
            and event.envelope.payload.get("delta_id") == delta_id
            for event in events
        ):
            raise ValueError("UNKNOWN_KNOWLEDGE_DELTA")
        acknowledgement_id = sha256_json(
            {"run_id": run_id, "worker_id": worker_id, "delta_id": delta_id}
        )
        if any(
            event.envelope.event_type == "KnowledgeDeltaAcknowledged"
            and event.envelope.payload.get("acknowledgement_id") == acknowledgement_id
            for event in events
        ):
            return False
        self._state.append_event(
            AppendEventCommand(
                "KnowledgeDeltaAcknowledged",
                worker_id,
                run_id,
                None,
                {
                    "acknowledgement_id": acknowledgement_id,
                    "worker_id": worker_id,
                    "delta_id": delta_id,
                },
            )
        )
        return True


def publication_delta(
    contribution_id: str,
    run_id: str,
    current: EpochPair,
    new_base_epoch: str,
    module: ArtifactReference,
    module_name: str,
    name: str,
    complete_type: str,
    dependencies: tuple[str, ...],
    assumptions: tuple[str, ...],
    axioms: tuple[str, ...],
    evidence_links: tuple[str, ...],
    sequence: int,
) -> PublicationDelta:
    new_epoch = EpochPair(new_base_epoch, current.knowledge_epoch + 1)
    declaration_id = sha256_json({"name": name, "module": module.content_hash})
    delta_id = sha256_json({"declaration": declaration_id, "epoch": new_epoch})
    prepared = AppendEventCommand(
        "PromotionPrepared",
        "promotion_service",
        run_id,
        None,
        {
            "contribution_id": contribution_id,
            "module": module_name,
            "content_hash": module.content_hash,
            "base_epoch": new_epoch.base_epoch,
            "knowledge_epoch": new_epoch.knowledge_epoch,
            "declaration_id": declaration_id,
            "delta_id": delta_id,
        },
    )
    declaration = AppendEventCommand(
        "DeclarationPublished",
        "promotion_service",
        run_id,
        None,
        {
            "declaration_id": declaration_id,
            "name": name,
            "type": complete_type,
            "content_hash": module.content_hash,
            "contribution_id": contribution_id,
            "module": module_name,
            "dependencies": _items(dependencies),
            "assumptions": _items(assumptions),
            "axioms": _items(axioms),
            "evidence_links": _items(evidence_links),
            "publication_sequence": sequence,
        },
    )
    delta = AppendEventCommand(
        "KnowledgeDeltaPublished",
        "promotion_service",
        run_id,
        None,
        {
            "delta_id": delta_id,
            "base_epoch": new_epoch.base_epoch,
            "knowledge_epoch": new_epoch.knowledge_epoch,
            "declaration_id": declaration_id,
            "contribution_id": contribution_id,
            "previous_base_epoch": current.base_epoch,
            "previous_knowledge_epoch": current.knowledge_epoch,
            "fully_qualified_name": name,
            "complete_type": complete_type,
            "module": module_name,
            "dependencies": _items(dependencies),
            "assumptions": _items(assumptions),
            "axioms": _items(axioms),
            "evidence_links": _items(evidence_links),
            "publication_sequence": sequence,
        },
    )
    return PublicationDelta(new_epoch, prepared, declaration, delta)


def _items(values: tuple[str, ...]) -> list[JsonValue]:
    return [item for item in values]


def research_name(candidate: str, digest: str, namespace: str = "AizimSmoke.Research") -> str:
    stem = re.sub(r"[^A-Za-z0-9_]", "_", candidate)
    prefix = "candidate_" if not stem or stem[0].isdigit() else ""
    return f"{namespace}.{prefix}{stem}_{digest[:16]}"


def _delta(payload: Mapping[str, object]) -> PublishedKnowledgeDelta:
    return PublishedKnowledgeDelta(
        _text(payload, "delta_id"),
        EpochPair(
            _text(payload, "previous_base_epoch"), _integer(payload, "previous_knowledge_epoch")
        ),
        EpochPair(_text(payload, "base_epoch"), _integer(payload, "knowledge_epoch")),
        _text(payload, "declaration_id"),
        _text(payload, "fully_qualified_name"),
        _text(payload, "complete_type"),
        _text(payload, "module"),
        _strings(payload, "dependencies"),
        _strings(payload, "assumptions"),
        _strings(payload, "axioms"),
        _strings(payload, "evidence_links"),
        _text(payload, "contribution_id"),
        _integer(payload, "publication_sequence"),
    )


def _text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if type(value) is not str or not value:
        raise ValueError("INVALID_KNOWLEDGE_DELTA")
    return value


def _integer(payload: Mapping[str, object], field: str) -> int:
    value = payload.get(field)
    if type(value) is not int or value < 0:
        raise ValueError("INVALID_KNOWLEDGE_DELTA")
    return value


def _strings(payload: Mapping[str, object], field: str) -> tuple[str, ...]:
    value = payload.get(field)
    if not isinstance(value, (list, tuple)) or any(
        type(item) is not str or not item for item in value
    ):
        raise ValueError("INVALID_KNOWLEDGE_DELTA")
    return cast(tuple[str, ...], tuple(value))
