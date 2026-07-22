from __future__ import annotations

from pathlib import Path
from typing import Final

from aizim.domain import compute_environment_fingerprint
from aizim.gateway import GatewayTool

ALLOWED_IMPORTS: Final = ("Std",)
PROVER_A_TOOLS: Final = (
    GatewayTool.LEAN_GOAL,
    GatewayTool.LEAN_MULTI_ATTEMPT,
    GatewayTool.DOCUMENT_APPLY,
    GatewayTool.CONTRIBUTION_SUBMIT,
    GatewayTool.KNOWLEDGE_READ,
)
PROVER_B_WAIT_TOOLS: Final = (
    GatewayTool.LEAN_GOAL,
    GatewayTool.KNOWLEDGE_READ,
)
PROVER_B_PROVE_TOOLS: Final = (
    GatewayTool.DOCUMENT_APPLY,
    GatewayTool.CONTRIBUTION_SUBMIT,
)
PROOF_RUN_TOOLS: Final = tuple(
    dict.fromkeys((*PROVER_A_TOOLS, *PROVER_B_WAIT_TOOLS, *PROVER_B_PROVE_TOOLS))
)


def environment_fingerprint(project_root: Path) -> str:
    return compute_environment_fingerprint(
        project_root,
        {"Std": "lean-toolchain"},
        {"allowed_imports": ALLOWED_IMPORTS, "lean_runtime": "shared"},
    )


def cache_state(project_root: Path) -> str:
    return "warm" if (project_root / ".lake").is_dir() else "cold"
