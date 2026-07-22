from __future__ import annotations

import secrets
from datetime import UTC, datetime

from aizim.domain import sha256_json
from aizim.domain.serialization import JsonValue
from aizim.state import AppendEventCommand, StateService
from aizim.state.documents import DocumentState
from aizim.state.events import utc_now

from .models import LeanRuntimeError, WorkerSession


def record_formal_action(
    state: StateService,
    document: DocumentState,
    session: WorkerSession,
    input_payload: dict[str, JsonValue],
    output_hash: str,
    verdict: str,
    started_at: datetime,
) -> None:
    state.append_event(
        AppendEventCommand(
            "FormalActionRecorded",
            "lean_runtime",
            session.run_id,
            None,
            {
                "action_id": secrets.token_hex(16),
                "action_kind": _action_kind(input_payload),
                "worker_id": session.worker_id,
                "document_id": document.document_id,
                "input_hash": sha256_json(input_payload),
                "output_hash": output_hash,
                "verdict": verdict,
                "document_version": document.file_version,
                "base_epoch": document.epoch_pair.base_epoch,
                "knowledge_epoch": document.epoch_pair.knowledge_epoch,
                "started_at": _timestamp(started_at),
                "completed_at": _timestamp(),
            },
        )
    )


def _action_kind(payload: dict[str, JsonValue]) -> str:
    operation = payload.get("operation")
    kinds = {
        "lean_diagnostic_messages": "diagnostics",
        "lean_goal": "goal",
        "lean_multi_attempt": "trial",
    }
    if type(operation) is not str or operation not in kinds:
        raise LeanRuntimeError("INVALID_LEAN_REQUEST")
    return kinds[operation]


def _timestamp(value: datetime | None = None) -> str:
    current = utc_now() if value is None else value
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")
