from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from .events import EventEnvelope

PROJECTION_NAMES: Final = (
    "project",
    "runs",
    "artifacts",
    "evaluations",
    "schedules",
    "workers",
    "worker_cursors",
    "epochs",
    "leases",
    "documents",
    "formal_actions",
    "contributions",
    "verified_declarations",
    "knowledge_deltas",
    "knowledge_acknowledgements",
    "denials",
    "resources",
    "alignment_reviews",
    "interventions",
)
AUDIT_PROJECTIONS: Final = frozenset({"denials"})


@dataclass(frozen=True, slots=True)
class ProjectionRecord:
    projection_name: str
    entity_id: str
    version: int
    state_json: bytes


type ProjectionReducer = Callable[
    [tuple[ProjectionRecord, ...], EventEnvelope], tuple[ProjectionRecord, ...]
]


def apply_event(
    snapshots: tuple[ProjectionRecord, ...], event: EventEnvelope
) -> tuple[ProjectionRecord, ...]:
    from .reducers import apply_event as reduce_event

    return reduce_event(snapshots, event)
