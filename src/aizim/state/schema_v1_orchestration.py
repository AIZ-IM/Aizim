from __future__ import annotations

from collections.abc import Callable

from .schema_v1_validation import integer_payload, sha256_payload


def extend[T](codecs: dict[str, T], codec: Callable[..., T]) -> None:
    codecs.update(
        {
            "ScheduleProposed": codec(("directive_id", "worker_id", "execution_id"), ("role",)),
            "WorkerCursorSaved": codec(
                (
                    "directive_id",
                    "worker_id",
                    "execution_id",
                    "last_event_sequence",
                    "remaining_budget",
                    "terminal",
                ),
                (
                    "last_acknowledged_delta",
                    "lease_id",
                    "document_id",
                    "document_version",
                    "base_epoch",
                    "knowledge_epoch",
                ),
                {
                    "last_event_sequence": integer_payload,
                    "remaining_budget": integer_payload,
                    "terminal": lambda value: type(value) is bool,
                    "document_version": integer_payload,
                    "base_epoch": sha256_payload,
                    "knowledge_epoch": integer_payload,
                },
            ),
            "WorkerTimedOut": codec(("worker_id", "execution_id"), ("reason_code",)),
            "AgentRunCompleted": codec(
                (
                    "worker_id",
                    "execution_id",
                    "status",
                    "transport_event_hash",
                    "final_message_hash",
                    "exit_code",
                ),
                ("policy_hash",),
                {
                    "transport_event_hash": sha256_payload,
                    "final_message_hash": sha256_payload,
                    "exit_code": integer_payload,
                    "policy_hash": sha256_payload,
                },
            ),
            "PromotionVerificationRecorded": codec(
                (
                    "contribution_id",
                    "diagnostics_hash",
                    "diagnostics_verdict",
                    "build_hash",
                    "build_verdict",
                    "axiom_verification_hash",
                    "axiom_verification_verdict",
                ),
                ("source_scan_hash", "source_scan_verdict"),
                {
                    "diagnostics_hash": sha256_payload,
                    "build_hash": sha256_payload,
                    "axiom_verification_hash": sha256_payload,
                    "source_scan_hash": sha256_payload,
                },
            ),
            "LeanRuntimeStopped": codec(("runtime_id",)),
            "ArtifactRegistered": codec(
                ("artifact_name", "content_hash", "relative_path", "media_type", "byte_length"),
                validators={"content_hash": sha256_payload, "byte_length": integer_payload},
            ),
            "FormalTraceSealed": codec(("cutoff_kind",)),
            "EvaluationTransitionRejected": codec(
                ("field", "requested_hash", "reason_code"),
                validators={"requested_hash": sha256_payload},
            ),
        }
    )
