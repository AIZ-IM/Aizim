from __future__ import annotations

from aizim import foundation_evidence as fe


def durable_start(
    events: tuple[fe.EventEvidence, ...], manifest: fe.JsonObject, before_sequence: int
) -> bool:
    pair = manifest.get("epoch_pair")
    history = tuple(
        event
        for event in events
        if event.sequence < before_sequence
        and event.event_type in {"ProjectInitialized", "KnowledgeDeltaPublished"}
    )
    if type(pair) is not dict or not history or history[0].event_type != "ProjectInitialized":
        return False
    base = history[0].payload.get("base_epoch")
    knowledge = history[0].payload.get("knowledge_epoch")
    if not fe.is_hash(base) or type(knowledge) is not int or knowledge != 0:
        return False
    for event in history[1:]:
        payload = event.payload
        next_base, next_knowledge = payload.get("base_epoch"), payload.get("knowledge_epoch")
        if (
            event.event_type != "KnowledgeDeltaPublished"
            or payload.get("previous_base_epoch") != base
            or payload.get("previous_knowledge_epoch") != knowledge
            or type(next_knowledge) is not int
            or next_knowledge != knowledge + 1
            or not fe.is_hash(next_base)
        ):
            return False
        base, knowledge = next_base, next_knowledge
    return pair == {"base_epoch": base, "knowledge_epoch": knowledge}
