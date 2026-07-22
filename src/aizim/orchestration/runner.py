from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

from aizim.config import AizimConfig, load_config
from aizim.config.model import LeanRuntimeMode
from aizim.lean.project import smoke_base_epoch
from aizim.runtime.layout import ProjectLayout
from aizim.state import AppendEventCommand, StateService, StateServiceConfig

from .conductor import ResearchConductor, SharedRunResult
from .resources import ResourceGovernor


class RunError(RuntimeError):
    pass


def run_autonomous_shared(project: Path, backend: str) -> int:
    try:
        config, result = asyncio.run(_run(project, backend))
    except Exception:
        print("aizim run: autonomous-shared run failed", file=sys.stderr)
        return 2
    _print_summary(config, result, backend)
    return 0


async def _run(project: Path, backend: str) -> tuple[AizimConfig, SharedRunResult]:
    if backend != "fake":
        raise RunError("BACKEND_UNAVAILABLE")
    layout = ProjectLayout.from_lean_project(project)
    layout.prepare_runtime()
    layout.validate_state_lock_entry()
    layout.validate_database_entry()
    config = load_config(layout.root)
    if config.run.lean_runtime is not LeanRuntimeMode.SHARED:
        raise RunError("ISOLATED_RUNTIME_UNAVAILABLE")
    state = StateService(StateServiceConfig(layout.root, "autonomous-shared"))
    try:
        layout.secure_database_permissions()
        _initialize(state, layout.root)
        governor = ResourceGovernor(
            config.resources, disk_free=lambda root: shutil.disk_usage(root).free
        )
        fixtures = _fixture_root()
        result = await ResearchConductor(state, layout.root, layout.root, governor).run_fake(
            fixtures / "prover_a.json", fixtures / "prover_b.json"
        )
    finally:
        state.close()
    return config, result


def _initialize(state: StateService, project_root: Path) -> None:
    if any(record.envelope.event_type == "ProjectInitialized" for record in state.query_events()):
        return
    state.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {
                "project_id": project_root.name,
                "base_epoch": smoke_base_epoch(project_root),
                "knowledge_epoch": 0,
            },
        )
    )


def _fixture_root() -> Path:
    return Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "fake_workers"


def _print_summary(config: AizimConfig, result: SharedRunResult, backend: str) -> None:
    lines = (
        "AIZIM RUN PASS",
        f"backend={backend}",
        f"participation={config.run.formal_participation.value}",
        f"runtime={config.run.lean_runtime.value}",
        f"proof_workers={config.resources.max_proof_workers}",
        f"lsp_instances={config.resources.lsp_instances}",
        f"human_interventions={config.run.human_interventions}",
        f"start_knowledge_epoch={result.start_knowledge_epoch}",
        f"end_knowledge_epoch={result.end_knowledge_epoch}",
        f"verified_declarations={result.verified_declarations}",
    )
    print("\n".join(lines))
