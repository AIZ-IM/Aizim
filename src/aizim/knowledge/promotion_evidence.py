from __future__ import annotations

from aizim.domain import sha256_json
from aizim.state import AppendEventCommand, StateService

from .promotion_types import PromotionEvidence

_ALLOWED_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})


def record_verification(
    state: StateService, run_id: str, contribution_id: str, evidence: PromotionEvidence
) -> None:
    diagnostics_hash = evidence.diagnostics_response_hash or sha256_json(
        {"diagnostics": evidence.diagnostics}
    )
    build_hash = evidence.build_response_hash or sha256_json({"success": evidence.build_success})
    axiom_hash = evidence.axiom_response_hash or sha256_json({"axioms": evidence.axioms})
    trusted_source_scan = evidence.axiom_response_hash is not None
    state.append_event(
        AppendEventCommand(
            "PromotionVerificationRecorded",
            "promotion_service",
            run_id,
            None,
            {
                "contribution_id": contribution_id,
                "diagnostics_hash": diagnostics_hash,
                "diagnostics_verdict": "pass" if not evidence.diagnostics else "failed",
                "build_hash": build_hash,
                "build_verdict": "pass" if evidence.build_success else "failed",
                "source_scan_hash": axiom_hash,
                "source_scan_verdict": (
                    "pass"
                    if trusted_source_scan
                    and not evidence.source_scan_warnings
                    and set(evidence.axioms) <= _ALLOWED_AXIOMS
                    else "failed"
                ),
                "axiom_verification_hash": axiom_hash,
                "axiom_verification_verdict": (
                    "pass" if set(evidence.axioms) <= _ALLOWED_AXIOMS else "failed"
                ),
            },
        )
    )
