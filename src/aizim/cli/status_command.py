from __future__ import annotations

import json
import sys
from pathlib import Path

from aizim.domain.serialization import JsonValue
from aizim.runtime.layout import LayoutError, ProjectLayout

from .state_client import ProjectionDocument, StateClientError, load_projections

_COLLECTIONS = {
    "run": "runs",
    "workers": "workers",
    "leases": "leases",
    "candidates": "contributions",
    "verified_declarations": "verified_declarations",
    "denied_capabilities": "denials",
    "resources": "resources",
}


def _row(record: ProjectionDocument) -> dict[str, JsonValue]:
    return {
        "entity_id": record.entity_id,
        "state": record.state,
        "version": record.version,
    }


def status_document(records: tuple[ProjectionDocument, ...]) -> dict[str, JsonValue]:
    grouped = {
        name: tuple(record for record in records if record.projection_name == projection)
        for name, projection in _COLLECTIONS.items()
    }
    default_epoch: dict[str, JsonValue] = {"knowledge_epoch": 0}
    epoch: JsonValue = next(
        (record.state for record in records if record.projection_name == "epochs"),
        default_epoch,
    )
    document: dict[str, JsonValue] = {
        "run": [_row(record) for record in grouped["run"]],
        "workers": [_row(record) for record in grouped["workers"]],
        "leases": [_row(record) for record in grouped["leases"]],
        "epochs": epoch,
        "candidates": [_row(record) for record in grouped["candidates"]],
        "verified_declarations": [
            _row(record) for record in grouped["verified_declarations"]
        ],
        "denied_capabilities": [
            _row(record) for record in grouped["denied_capabilities"]
        ],
        "resources": [_row(record) for record in grouped["resources"]],
    }
    return document


def run_status(project: Path, as_json: bool) -> int:
    try:
        layout = ProjectLayout.from_lean_project(project)
        layout.validate_runtime()
        document = status_document(load_projections(layout))
    except (LayoutError, StateClientError, OSError, RuntimeError, ValueError):
        print("aizim status: state is unavailable", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    epoch = document["epochs"]
    knowledge_epoch = epoch.get("knowledge_epoch", 0) if type(epoch) is dict else 0
    print(f"knowledge_epoch {knowledge_epoch}")
    for name in _COLLECTIONS:
        values = document[name]
        print(f"{name} {len(values) if type(values) is list else 0}")
    return 0
