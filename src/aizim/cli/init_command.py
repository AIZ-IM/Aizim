from __future__ import annotations

import sys
from pathlib import Path

from aizim.runtime.layout import ProjectLayout
from aizim.state import StateService, StateServiceConfig


def run_init(project: Path) -> int:
    try:
        layout = ProjectLayout.from_lean_project(project)
        layout.prepare_runtime()
        layout.validate_state_lock_entry()
        layout.validate_database_entry()
        with StateService(StateServiceConfig(layout.root, "init-session")):
            pass
        layout.secure_database_permissions()
    except Exception:
        print("aizim init: initialization failed", file=sys.stderr)
        return 2
    print(f"Initialized Aizim state at {layout.state_root}")
    return 0
