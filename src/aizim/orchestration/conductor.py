from __future__ import annotations

import asyncio  # noqa: ANYIO_OK - preserves existing concurrent worker cancellation semantics
import secrets
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path

from aizim.agents import FakeAgentBackend
from aizim.config.model import PROOF_WORKER_TIMEOUT_SECONDS, LeanRuntimeMode
from aizim.domain import AgentRole, EpochPair
from aizim.gateway import connect_gateway
from aizim.gateway.socket_alias import ProjectSocketAlias
from aizim.lean.broker_knowledge import current_epoch
from aizim.orchestration.worker_host import WorkerExecutionHost, _HostFactories
from aizim.state import AppendEventCommand, StateService

from .evaluation_contract import (
    PROVER_A_TOOLS,
    PROVER_B_PROVE_TOOLS,
    PROVER_B_WAIT_TOOLS,
)
from .promotion_consumer import PromotionConsumer
from .resources import ResourceGovernor
from .run_identity import candidate_name
from .run_validation import (
    SharedRunInvariantError,
    first_published_delta,
    validate_shared_completion,
)
from .worker import WorkerDirective
from .worker_authority import WorkerBackend
from .worker_cursor import WorkerCursor

type WorkerBackendFactory = Callable[[str, int, dict[str, str] | None], WorkerBackend]


class _ResearchConductorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SharedRunResult:
    run_id: str
    start_knowledge_epoch: int
    end_knowledge_epoch: int
    verified_declarations: int


class ResearchConductor:
    def __init__(
        self,
        state: StateService,
        project_root: Path,
        smoke_root: Path,
        governor: ResourceGovernor,
    ) -> None:
        if type(state) is not StateService or not isinstance(project_root, Path):
            raise _ResearchConductorError("INVALID_RESEARCH_CONDUCTOR")
        self._state, self._project_root = state, project_root
        self._smoke_root, self._governor = smoke_root, governor

    async def run_fake(
        self,
        prover_a: Path,
        prover_b: Path,
        run_id: str | None = None,
        *,
        record_completion: bool = True,
        start_epoch: EpochPair | None = None,
    ) -> SharedRunResult:
        def factory(
            worker_id: str, round_index: int, delta: dict[str, str] | None
        ) -> WorkerBackend:
            fixture = prover_a if worker_id == "prover-a" else prover_b
            return self._backend(fixture, round_index, delta)

        return await self.run_two_worker(
            factory,
            run_id,
            record_completion=record_completion,
            start_epoch=start_epoch,
            prewarm_runtime=True,
        )

    async def run_two_worker(
        self,
        backend_factory: WorkerBackendFactory,
        run_id: str | None = None,
        *,
        record_completion: bool = True,
        start_epoch: EpochPair | None = None,
        prewarm_runtime: bool = False,
    ) -> SharedRunResult:
        self._governor.validate(self._project_root, LeanRuntimeMode.SHARED)
        created = run_id is None
        if run_id is None:
            run_id = f"shared-{secrets.token_hex(8)}"
        selected_epoch = current_epoch(self._state) if start_epoch is None else start_epoch
        if type(selected_epoch) is not EpochPair or selected_epoch != current_epoch(self._state):
            raise SharedRunInvariantError("START_SNAPSHOT_MISMATCH")
        start = selected_epoch.knowledge_epoch
        if created:
            self._state.append_event(
                AppendEventCommand(
                    "RunCreated", "research_conductor", run_id, None, {"status": "running"}
                )
            )
        host: WorkerExecutionHost | None = None
        try:
            opened = await WorkerExecutionHost._open_with_factories(
                self._state,
                self._project_root,
                self._smoke_root,
                self._governor,
                run_id,
                _HostFactories(ProjectSocketAlias, PromotionConsumer),
            )
            host = opened
            if prewarm_runtime:
                await opened.prewarm()
            await _run_workers(
                opened.run(
                    _directive(run_id, "prover-a", 0),
                    backend_factory("prover-a", 0, None),
                ),
                opened.run(
                    _directive(run_id, "prover-b", 0),
                    backend_factory("prover-b", 0, None),
                ),
            )
            delta = first_published_delta(self._state, run_id, start)
            await opened.run(
                _directive(run_id, "prover-b", 1),
                backend_factory(
                    "prover-b",
                    1,
                    {"fully_qualified_name": delta.fully_qualified_name, "module": delta.module},
                ),
            )
            await opened.wait_for_epoch(start + 2, PROOF_WORKER_TIMEOUT_SECONDS)
            end = current_epoch(self._state).knowledge_epoch
            declarations = validate_shared_completion(self._state, run_id, start, end, delta)
            if record_completion:
                self._state.append_event(
                    AppendEventCommand(
                        "RunCompleted", "research_conductor", run_id, None, {"outcome": "PASS"}
                    )
                )
            return SharedRunResult(run_id, start, end, declarations)
        except BaseException:  # noqa: BROAD_EXCEPT_OK - durable run-abort boundary
            self._state.append_event(
                AppendEventCommand(
                    "RunAborted", "research_conductor", run_id, None, {"reason_code": "RUN_FAILED"}
                )
            )
            raise
        finally:
            if host is not None:
                await host.aclose()

    @staticmethod
    def _backend(
        fixture: Path, round_index: int, delta: dict[str, str] | None = None
    ) -> FakeAgentBackend:
        return FakeAgentBackend.from_fixture(
            fixture, connect=connect_gateway, cursor={}, round_index=round_index, known_delta=delta
        )


def _directive(run_id: str, worker_id: str, round_index: int) -> WorkerDirective:
    name = candidate_name(run_id, worker_id)
    source = f"import Std\n\ntheorem {name} (n : Nat) : n + 0 = n := by\n  sorry\n".encode()
    operations = (
        PROVER_A_TOOLS
        if worker_id == "prover-a"
        else PROVER_B_WAIT_TOOLS
        if round_index == 0
        else PROVER_B_PROVE_TOOLS
    )
    return WorkerDirective(
        f"directive-{worker_id}",
        worker_id,
        AgentRole.PROOF_EXPLORER,
        source,
        12,
        PROOF_WORKER_TIMEOUT_SECONDS,
        operations,
    )


async def _run_workers(*workers: Coroutine[None, None, WorkerCursor]) -> None:
    tasks = tuple(asyncio.create_task(worker) for worker in workers)
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
