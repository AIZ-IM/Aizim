from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .events import EventEnvelope


@dataclass(frozen=True, slots=True)
class ProjectionRecord:
    projection_name: str
    entity_id: str
    version: int
    state_json: bytes


type ProjectionReducer = Callable[
    [tuple[ProjectionRecord, ...], EventEnvelope], tuple[ProjectionRecord, ...]
]
