from __future__ import annotations

from typing import Final

from .events import EventEnvelope
from .projection_types import ProjectionRecord, ProjectionReducer

__all__ = [
    "AUDIT_PROJECTIONS",
    "PROJECTION_NAMES",
    "ProjectionRecord",
    "ProjectionReducer",
    "apply_event",
]

PROJECTION_NAMES: Final = (
    "project",
    "runs",
    "artifacts",
    "evaluations",
    "schedules",
    "controller",
    "workers",
    "worker_roster",
    "worker_assignments",
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


def apply_event(
    snapshots: tuple[ProjectionRecord, ...], event: EventEnvelope
) -> tuple[ProjectionRecord, ...]:
    from .reducers import apply_event as reduce_event

    return reduce_event(snapshots, event)
