from __future__ import annotations

from aizim.knowledge import KnowledgeReader, PublishedKnowledgeDelta
from aizim.state import StateService

from .run_identity import contribution_id


class SharedRunInvariantError(RuntimeError):
    pass


def first_published_delta(
    state: StateService, run_id: str, start_epoch: int
) -> PublishedKnowledgeDelta:
    candidates = tuple(
        delta
        for delta in KnowledgeReader(state).read(start_epoch)
        if delta.new_epoch.knowledge_epoch == start_epoch + 1
    )
    expected = contribution_id(run_id, "prover-a")
    if len(candidates) != 1 or candidates[0].contribution_id != expected:
        raise SharedRunInvariantError("FIRST_DELTA_NOT_PROVER_A")
    submitted_by_a = any(
        record.envelope.event_type == "ContributionSubmitted"
        and record.envelope.payload.get("contribution_id") == expected
        and record.envelope.payload.get("worker_id") == "prover-a"
        for record in state.query_events(run_id)
    )
    if not submitted_by_a:
        raise SharedRunInvariantError("FIRST_DELTA_NOT_PROVER_A")
    return candidates[0]


def validate_shared_completion(
    state: StateService,
    run_id: str,
    start_epoch: int,
    end_epoch: int,
    first_delta: PublishedKnowledgeDelta,
) -> int:
    events = state.query_events(run_id)
    declarations = tuple(
        record.envelope.payload.get("contribution_id")
        for record in events
        if record.envelope.event_type == "DeclarationPublished"
    )
    deltas = KnowledgeReader(state).read(start_epoch)
    acknowledged = any(
        record.envelope.event_type == "KnowledgeDeltaAcknowledged"
        and record.envelope.payload.get("worker_id") == "prover-b"
        and record.envelope.payload.get("delta_id") == first_delta.delta_id
        for record in events
    )
    expected = (
        contribution_id(run_id, "prover-a"),
        contribution_id(run_id, "prover-b"),
    )
    valid = (
        end_epoch == start_epoch + 2
        and declarations == expected
        and len(deltas) == 2
        and deltas[1].contribution_id == expected[1]
        and acknowledged
    )
    if not valid:
        raise SharedRunInvariantError("SHARED_RUN_INVARIANT_FAILED")
    return len(declarations)
