from __future__ import annotations

import sys
from pathlib import Path

from aizim.domain import sha256_file, sha256_json
from aizim.lean.document_io import DocumentIoError
from aizim.lean.project import smoke_base_epoch
from aizim.lean.source_layout import lakefile
from aizim.runtime.layout import ProjectLayout
from aizim.state import AppendEventCommand, StateService, StateServiceConfig


class InitIdentityError(RuntimeError):
    pass


def run_init(project: Path) -> int:
    try:
        layout = ProjectLayout.from_lean_project(project)
        layout.prepare_runtime()
        layout.validate_state_lock_entry()
        layout.validate_database_entry()
        with StateService(StateServiceConfig(layout.root, "init-session")) as state:
            _ensure_project_identity(state, layout.root)
        layout.secure_database_permissions()
    except Exception:  # noqa: BROAD_EXCEPT_OK - scrub the public CLI error boundary
        print("aizim init: initialization failed", file=sys.stderr)
        return 2
    print(f"Initialized Aizim state at {layout.state_root}")
    return 0


def _ensure_project_identity(state: StateService, project: Path) -> None:
    identities = tuple(
        record.envelope
        for record in state.query_events()
        if record.envelope.event_type == "ProjectInitialized"
    )
    if not identities:
        state.append_event(
            AppendEventCommand(
                "ProjectInitialized",
                "supervisor",
                None,
                None,
                {
                    "project_id": project.name,
                    "base_epoch": _initial_base_epoch(project),
                    "knowledge_epoch": 0,
                },
            )
        )
        return
    if len(identities) != 1:
        raise InitIdentityError("PROJECT_IDENTITY_INVALID")
    payload = identities[0].payload
    base_epoch = payload.get("base_epoch")
    if (
        payload.get("project_id") != project.name
        or type(base_epoch) is not str
        or len(base_epoch) != 64
        or any(character not in "0123456789abcdef" for character in base_epoch)
        or payload.get("knowledge_epoch") != 0
    ):
        raise InitIdentityError("PROJECT_IDENTITY_INVALID")


def _initial_base_epoch(project: Path) -> str:
    try:
        return smoke_base_epoch(project)
    except DocumentIoError:
        return sha256_json(
            {
                "lakefile": sha256_file(lakefile(project)),
                "lean_toolchain": sha256_file(project / "lean-toolchain"),
            }
        )
