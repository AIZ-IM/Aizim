from __future__ import annotations

from collections.abc import Callable

from aizim.domain.controller_provider import is_controller_provider_id
from aizim.domain.serialization import JsonValue

from .schema_v1_validation import integer_payload, sha256_payload


def _positive_integer(value: JsonValue) -> bool:
    return type(value) is int and value > 0


def _enum(*values: str) -> Callable[[JsonValue], bool]:
    allowed = frozenset(values)
    return lambda value: type(value) is str and value in allowed


def extend[T](codecs: dict[str, T], codec: Callable[..., T]) -> None:
    codecs.update(
        {
            "ControllerConfigured": codec(
                ("controller_id", "provider"),
                ("model",),
                {"provider": is_controller_provider_id},
            ),
            "WorkerConfigured": codec(("worker_id", "role", "status")),
            "WorkerTaskAssigned": codec(
                (
                    "assignment_id",
                    "controller_id",
                    "worker_id",
                    "task",
                    "task_hash",
                    "task_version",
                ),
                validators={
                    "assignment_id": sha256_payload,
                    "task_hash": sha256_payload,
                    "task_version": lambda value: type(value) is int and value > 0,
                },
            ),
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
            "ControllerStarted": codec(
                (
                    "controller_id",
                    "controller_session_id",
                    "controller_version",
                    "provider",
                    "backend_version",
                    "executable_hash",
                ),
                validators={
                    "controller_version": _positive_integer,
                    "provider": is_controller_provider_id,
                    "executable_hash": sha256_payload,
                },
            ),
            "ControllerStopped": codec(
                ("controller_id", "controller_session_id", "reason_code"),
                validators={
                    "reason_code": _enum(
                        "OPERATOR_SIGNAL",
                        "PREFLIGHT_FAILED",
                        "CONTROLLER_FAILED",
                    )
                },
            ),
            "ControllerCrashed": codec(
                ("controller_id", "controller_session_id", "reason_code"),
                validators={"reason_code": _enum("UNCLEAN_SHUTDOWN")},
            ),
            "WorkerTaskClaimed": codec(
                (
                    "assignment_id",
                    "controller_id",
                    "controller_session_id",
                    "controller_version",
                    "worker_id",
                    "task_version",
                    "execution_id",
                ),
                validators={
                    "assignment_id": sha256_payload,
                    "controller_version": _positive_integer,
                    "task_version": _positive_integer,
                },
            ),
            "WorkerTaskDispatchPlanned": codec(
                (
                    "assignment_id",
                    "execution_id",
                    "directive_id",
                    "directive_artifact_hash",
                    "instruction_hash",
                    "budget",
                    "timeout_milliseconds",
                ),
                validators={
                    "assignment_id": sha256_payload,
                    "directive_artifact_hash": sha256_payload,
                    "instruction_hash": sha256_payload,
                    "budget": _positive_integer,
                    "timeout_milliseconds": _positive_integer,
                },
            ),
            "WorkerTaskCompleted": codec(
                ("assignment_id", "execution_id", "result_hash"),
                validators={
                    "assignment_id": sha256_payload,
                    "result_hash": sha256_payload,
                },
            ),
            "WorkerTaskFailed": codec(
                ("assignment_id", "execution_id", "reason_code"),
                validators={
                    "assignment_id": sha256_payload,
                    "reason_code": _enum(
                        "CONTROLLER_BLOCKED",
                        "CONTROLLER_REJECTED",
                        "CONTROLLER_DECISION_INVALID",
                        "CONTROLLER_TIMEOUT",
                        "WORKER_FAILED",
                        "WORKER_ROLE_UNSUPPORTED",
                    ),
                },
            ),
            "WorkerTaskInterrupted": codec(
                ("assignment_id", "execution_id", "reason_code"),
                validators={
                    "assignment_id": sha256_payload,
                    "reason_code": _enum("OPERATOR_SIGNAL", "CONTROLLER_RESTART"),
                },
            ),
        }
    )
