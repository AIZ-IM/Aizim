from __future__ import annotations

import asyncio

from aizim.agents import AgentResult
from aizim.async_lifecycle import await_cleanup
from aizim.domain import FileLease
from aizim.lean import DocumentBroker
from aizim.state import AppendEventCommand, StateService


def record_agent_result(
    state: StateService, run_id: str, execution_id: str, result: AgentResult
) -> None:
    state.append_event(
        AppendEventCommand(
            "AgentRunCompleted",
            "worker_runner",
            run_id,
            None,
            {
                "worker_id": result.worker_id,
                "execution_id": execution_id,
                "status": result.status,
                "transport_event_hash": result.transport_event_hash,
                "final_message_hash": result.final_message_hash,
                "exit_code": result.exit_code,
            },
        )
    )


async def release_worker_lease(
    broker: DocumentBroker,
    run_id: str,
    lease: FileLease,
    primary_failure: BaseException | None,
) -> None:
    cleanup = asyncio.create_task(broker.release_lease(run_id, lease.worker_id, lease.lease_id))
    try:
        interruption = await await_cleanup(cleanup)
    except BaseException:
        if primary_failure is None:
            raise
        return
    if interruption is not None and primary_failure is None:
        raise interruption
