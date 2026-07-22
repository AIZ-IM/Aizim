from __future__ import annotations

from aizim.config import AizimConfig
from aizim.modes.manifest import SMOKE_TEST_LIMITATION
from aizim.state import AppendEventCommand, StateService

from .conductor import SharedRunResult


def print_run_summary(config: AizimConfig, result: SharedRunResult, backend: str) -> None:
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
        SMOKE_TEST_LIMITATION,
    )
    print("\n".join(lines))


def record_completion(state: StateService, run_id: str) -> None:
    state.append_event(
        AppendEventCommand("RunCompleted", "evaluation_policy", run_id, None, {"outcome": "PASS"})
    )
