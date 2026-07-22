from __future__ import annotations

from aizim.domain import canonical_json
from aizim.state import (
    AppendEventCommand,
    PublicationQueueEntry,
    StateService,
)

from .artifacts import ArtifactStore


def quarantine_failure(
    state: StateService,
    artifacts: ArtifactStore,
    owner_id: str,
    entry: PublicationQueueEntry,
    run_id: str | None,
    reason: str,
    diagnostics: tuple[str, ...],
    axioms: tuple[str, ...],
) -> PublicationQueueEntry:
    artifact = artifacts.store(
        run_id or "unknown",
        "diagnostics",
        canonical_json(
            {"reason": reason, "diagnostics": list(diagnostics), "axioms": list(axioms)}
        ),
        "application/json",
    )
    return state.fail_promotion(
        entry.contribution_id,
        owner_id,
        AppendEventCommand(
            "PromotionFailed",
            "promotion_service",
            run_id,
            None,
            {
                "contribution_id": entry.contribution_id,
                "reason_code": reason,
                "artifact_hash": artifact.content_hash,
            },
        ),
    )
