from __future__ import annotations

import asyncio
import secrets
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from aizim.agents import FakeAgentBackend
from aizim.config.model import LeanRuntimeMode
from aizim.domain import AgentRole, sha256_bytes
from aizim.gateway import (
    CapabilityDependencies,
    CapabilityGateway,
    CapabilityIssuer,
    GatewayLimits,
    GatewaySessionBroker,
    connect_gateway,
)
from aizim.knowledge import ArtifactStore, KnowledgeReader, RuntimePromotionVerifier
from aizim.lean import DocumentBroker, SharedLeanRuntime
from aizim.lean.broker_knowledge import current_epoch
from aizim.state import AppendEventCommand, StateService
from aizim.state.events import utc_now

from .knowledge_stream import KnowledgeStream
from .promotion_consumer import PromotionConsumer
from .resources import ResourceGovernor
from .worker import WorkerDirective, WorkerRunner
from .worker_authority import BrokerWorkerAuthority
from .worker_gateway import WorkerGatewayActions

_ENVIRONMENT = "e" * 64


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
            raise ValueError("INVALID_RESEARCH_CONDUCTOR")
        self._state, self._project_root, self._smoke_root, self._governor = (
            state,
            project_root,
            smoke_root,
            governor,
        )

    async def run_fake(self, prover_a: Path, prover_b: Path) -> SharedRunResult:
        self._governor.validate(self._project_root, LeanRuntimeMode.SHARED)
        run_id = f"shared-{secrets.token_hex(8)}"
        start = current_epoch(self._state).knowledge_epoch
        self._state.append_event(
            AppendEventCommand(
                "RunCreated", "research_conductor", run_id, None, {"status": "running"}
            )
        )
        broker = DocumentBroker(self._project_root, self._state, smoke_root=self._smoke_root)
        runtime = SharedLeanRuntime(self._state, broker, run_id)
        knowledge = KnowledgeStream(self._state)
        artifacts = ArtifactStore(self._project_root)
        stop = asyncio.Event()
        actions = WorkerGatewayActions(
            self._state,
            broker,
            runtime,
            knowledge,
            artifacts,
            _ENVIRONMENT,
            lambda: consumer.notify_submission(),
        )
        gateway = CapabilityGateway(
            self._state,
            actions.targets(),
            GatewayLimits(200, 60.0, 200),
            CapabilityDependencies(utc_now, time.monotonic, lambda: secrets.token_hex(16)),
        )
        socket_root = TemporaryDirectory(prefix="aizim-gateway-")
        socket = Path(socket_root.name) / "gateway.sock"
        sessions = GatewaySessionBroker(socket, gateway=gateway)
        authority = BrokerWorkerAuthority(
            CapabilityIssuer(self._state), sessions, sha256_bytes(Path(sys.executable).read_bytes())
        )
        runner = WorkerRunner(
            self._state, broker, self._governor, authority, self._project_root, run_id
        )
        verifier = RuntimePromotionVerifier(runtime, broker, run_id)
        consumer = PromotionConsumer(self._state, artifacts, verifier, knowledge)
        consumer_task: asyncio.Task[None] | None = None
        failure_task: asyncio.Task[None] | None = None
        try:
            await sessions.start()
            consumer_task = asyncio.create_task(consumer.run(stop))
            failure_task = asyncio.create_task(consumer.wait_for_failure())
            first = asyncio.gather(
                runner.run(_directive("prover-a"), self._backend(prover_a, 0)),
                runner.run(_directive("prover-b"), self._backend(prover_b, 0)),
            )
            completed, _ = await asyncio.wait(
                (first, failure_task), return_when=asyncio.FIRST_COMPLETED
            )
            if failure_task in completed:
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
                await failure_task
            await first
            delta = next(
                item
                for item in KnowledgeReader(self._state).read(start)
                if item.new_epoch.knowledge_epoch == start + 1
            )
            await runner.run(
                _directive("prover-b"),
                self._backend(
                    prover_b,
                    1,
                    {"fully_qualified_name": delta.fully_qualified_name, "module": delta.module},
                ),
            )
            await consumer.wait_for_epoch(start + 2)
            end = current_epoch(self._state).knowledge_epoch
            declarations = sum(
                event.envelope.event_type == "DeclarationPublished"
                for event in self._state.query_events(run_id)
            )
            self._state.append_event(
                AppendEventCommand(
                    "RunCompleted", "research_conductor", run_id, None, {"outcome": "PASS"}
                )
            )
            return SharedRunResult(run_id, start, end, declarations)
        except BaseException:
            self._state.append_event(
                AppendEventCommand(
                    "RunAborted", "research_conductor", run_id, None, {"reason_code": "RUN_FAILED"}
                )
            )
            raise
        finally:
            if failure_task is not None:
                failure_task.cancel()
                with suppress(asyncio.CancelledError):
                    await failure_task
            stop.set()
            consumer.notify_submission()
            if consumer_task is not None:
                await consumer_task
            await sessions.aclose()
            await runtime.aclose()
            socket_root.cleanup()

    @staticmethod
    def _backend(
        fixture: Path, round_index: int, delta: dict[str, str] | None = None
    ) -> FakeAgentBackend:
        return FakeAgentBackend.from_fixture(
            fixture, connect=connect_gateway, cursor={}, round_index=round_index, known_delta=delta
        )


def _directive(worker_id: str) -> WorkerDirective:
    name = "a_add_zero" if worker_id == "prover-a" else "b_use_a"
    source = f"import Std\n\ntheorem {name} (n : Nat) : n + 0 = n := by\n  sorry\n".encode()
    return WorkerDirective(
        f"directive-{worker_id}", worker_id, AgentRole.PROOF_EXPLORER, source, 12, 30.0
    )
