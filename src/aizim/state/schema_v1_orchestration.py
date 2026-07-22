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
            "LeanRuntimeStopped": codec(("runtime_id",)),
        }
    )
