from __future__ import annotations

from aizim.domain import AgentRole, sha256_json
from aizim.state import AppendEventCommand, StateService


def record_schedule(
    state: StateService,
    run_id: str,
    directive_id: str,
    worker_id: str,
    execution_id: str,
    role: AgentRole,
) -> None:
    state.append_event(
        AppendEventCommand(
            "ScheduleProposed",
            "research_conductor",
            run_id,
            None,
            {
                "directive_id": directive_id,
                "worker_id": worker_id,
                "execution_id": execution_id,
                "role": role.value,
            },
        )
    )
    state.append_event(
        AppendEventCommand(
            "WorkerRegistered",
            "research_conductor",
            run_id,
            None,
            {"worker_id": worker_id, "role": role.value},
        )
    )


def record_started(state: StateService, run_id: str, worker_id: str, role: AgentRole) -> None:
    state.append_event(
        AppendEventCommand(
            "WorkerStarted",
            "worker_runner",
            run_id,
            None,
            {"worker_id": worker_id, "role": role.value},
        )
    )


def record_stopped(state: StateService, run_id: str, worker_id: str, reason: str) -> None:
    state.append_event(
        AppendEventCommand(
            "WorkerStopped",
            "worker_runner",
            run_id,
            None,
            {"worker_id": worker_id, "reason_code": reason},
        )
    )


def record_crashed(state: StateService, run_id: str, worker_id: str, reason: str) -> None:
    state.append_event(
        AppendEventCommand(
            "WorkerCrashed",
            "worker_runner",
            run_id,
            None,
            {
                "worker_id": worker_id,
                "reason_code": reason,
                "artifact_hash": sha256_json({"reason": reason}),
            },
        )
    )
