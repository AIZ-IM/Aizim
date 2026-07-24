from __future__ import annotations

import asyncio  # noqa: ANYIO_OK - composes the existing asyncio worker host
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from aizim.agents import AgentBackend
from aizim.domain import canonical_json, sha256_bytes, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.knowledge import ArtifactStore
from aizim.state import ProjectionRecord, StateService

from .codex_worker import CodexWorkspaceBackend
from .controller_backend import (
    CONTROLLER_PLAN_TIMEOUT_SECONDS,
    MAX_WORKER_BUDGET,
    MAX_WORKER_TIMEOUT_SECONDS,
    BlockedDecision,
    ControllerBackend,
    ControllerBackendError,
    ControllerContext,
    ControllerDecision,
    DispatchDecision,
    RejectDecision,
)
from .controller_execution import (
    ClaimedAssignment,
    ControllerExecutionError,
    claim_assignment,
    complete_assignment,
    fail_assignment,
    interrupt_assignment,
    record_dispatch_planned,
)
from .resources import ResourceGovernor
from .worker_authority import WorkerDirective
from .worker_gateway import controller_worker_tools
from .worker_host import WorkerExecutionHost

_DIRECTIVE_ID = re.compile(r"directive-[0-9a-f]{32}")
_EXECUTION_ID = re.compile(r"execution-[0-9a-f]{32}")
_CONTROLLER_ID = "primary"


@dataclass(frozen=True, slots=True)
class ControllerDispatcherDependencies:
    state: StateService
    project: Path
    worker_model: str
    governor: ResourceGovernor
    worker_backend: AgentBackend
    controller_backend: ControllerBackend
    directive_ids: Callable[[], str]


@dataclass(frozen=True, slots=True)
class ControllerRun:
    version: int
    session_id: str
    project_id: str


class ControllerDispatcher:
    def __init__(self, dependencies: ControllerDispatcherDependencies) -> None:
        self._dependencies = dependencies

    async def execute(
        self,
        claim: ClaimedAssignment,
        decision: ControllerDecision,
    ) -> None:
        match decision:
            case BlockedDecision():
                fail_assignment(self._dependencies.state, claim, "CONTROLLER_BLOCKED")
            case RejectDecision():
                fail_assignment(self._dependencies.state, claim, "CONTROLLER_REJECTED")
            case DispatchDecision() as dispatch:
                await self._dispatch(claim, dispatch)
            case unreachable:
                assert_never(unreachable)

    async def plan_and_execute(
        self,
        claim: ClaimedAssignment,
        context: ControllerContext,
    ) -> None:
        try:
            decision = await asyncio.wait_for(
                self._dependencies.controller_backend.plan(context),
                CONTROLLER_PLAN_TIMEOUT_SECONDS,
            )
            await self.execute(claim, decision)
        except TimeoutError:
            fail_assignment(self._dependencies.state, claim, "CONTROLLER_TIMEOUT")
        except ControllerBackendError:
            fail_assignment(self._dependencies.state, claim, "CONTROLLER_DECISION_INVALID")
        except asyncio.CancelledError:
            interrupt_assignment(self._dependencies.state, claim, "OPERATOR_SIGNAL")
            raise

    async def _dispatch(
        self,
        claim: ClaimedAssignment,
        decision: DispatchDecision,
    ) -> None:
        operations = controller_worker_tools(claim.role)
        if not operations:
            fail_assignment(self._dependencies.state, claim, "WORKER_ROLE_UNSUPPORTED")
            return
        directive_id = self._dependencies.directive_ids()
        if type(directive_id) is not str or _DIRECTIVE_ID.fullmatch(directive_id) is None:
            fail_assignment(self._dependencies.state, claim, "WORKER_FAILED")
            return
        name = f"aizim_assignment_{claim.assignment_id[:16]}"
        initial_source = (f"import Std\n\ntheorem {name} : True := by\n  sorry\n").encode()
        directive = WorkerDirective(
            directive_id=directive_id,
            worker_id=claim.worker_id,
            role=claim.role,
            initial_source=initial_source,
            budget=decision.budget,
            timeout_seconds=decision.timeout_seconds,
            operations=operations,
        )
        execution_run_id = f"task-{claim.execution_id}"
        artifact = ArtifactStore(self._dependencies.project).store(
            execution_run_id,
            "controller-directive",
            canonical_json(
                {
                    "budget": directive.budget,
                    "directive_id": directive.directive_id,
                    "initial_source_hash": sha256_bytes(initial_source),
                    "instruction": decision.instruction,
                    "operations": tuple(operation.value for operation in operations),
                    "role": directive.role.value,
                    "timeout_seconds": directive.timeout_seconds,
                    "worker_id": directive.worker_id,
                }
            ),
            "application/json",
        )
        record_dispatch_planned(
            self._dependencies.state,
            claim,
            directive_id=directive.directive_id,
            directive_artifact_hash=artifact.content_hash,
            instruction_hash=sha256_bytes(decision.instruction.encode()),
            budget=directive.budget,
            timeout_milliseconds=max(1, math.ceil(directive.timeout_seconds * 1_000)),
        )
        backend = CodexWorkspaceBackend(
            self._dependencies.worker_backend,
            self._dependencies.project,
            self._dependencies.worker_model,
            decision.instruction,
        )
        try:
            host = await WorkerExecutionHost.open(
                self._dependencies.state,
                self._dependencies.project,
                self._dependencies.project,
                self._dependencies.governor,
                execution_run_id,
            )
            try:
                cursor = await host.run(directive, backend)
            finally:
                await host.aclose()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BROAD_EXCEPT_OK - worker execution boundary
            fail_assignment(self._dependencies.state, claim, "WORKER_FAILED")
            return
        complete_assignment(self._dependencies.state, claim, sha256_json(cursor))


def claim_next(
    state: StateService,
    run: ControllerRun,
    execution_ids: Callable[[], str],
) -> ClaimedAssignment | None:
    configured = state.query_projection("controller", _CONTROLLER_ID)
    if configured is None or configured.version != run.version:
        raise ControllerExecutionError("CONTROLLER_VERSION_STALE")
    for worker in sorted(state.projections("worker_roster"), key=lambda item: item.entity_id):
        assignment = state.query_projection("worker_assignments", worker.entity_id)
        if assignment is None:
            continue
        assignment_id = _text(_payload(assignment), "assignment_id")
        if state.query_projection("worker_executions", assignment_id) is not None:
            continue
        execution_id = execution_ids()
        if type(execution_id) is not str or _EXECUTION_ID.fullmatch(execution_id) is None:
            raise ControllerExecutionError("EXECUTION_ID_INVALID")
        return claim_assignment(
            state,
            worker_id=worker.entity_id,
            controller_session_id=run.session_id,
            controller_version=run.version,
            execution_id=execution_id,
        )
    return None


def controller_context(
    state: StateService,
    claim: ClaimedAssignment,
    run: ControllerRun,
) -> ControllerContext:
    epoch = state.query_projection("epochs", "global")
    if epoch is None:
        raise ControllerExecutionError("PROJECT_EPOCH_UNAVAILABLE")
    epoch_state: JsonValue = json.loads(epoch.state_json)
    if type(epoch_state) is not dict:
        raise ControllerExecutionError("PROJECT_EPOCH_INVALID")
    knowledge_epoch = epoch_state.get("knowledge_epoch")
    if type(knowledge_epoch) is not int or knowledge_epoch < 0:
        raise ControllerExecutionError("PROJECT_EPOCH_INVALID")
    return ControllerContext(
        assignment_id=claim.assignment_id,
        task_version=claim.task_version,
        task=claim.task,
        worker_id=claim.worker_id,
        role=claim.role,
        project_id=run.project_id,
        base_epoch=_text(epoch_state, "base_epoch"),
        knowledge_epoch=knowledge_epoch,
        allowed_operations=tuple(
            operation.value for operation in controller_worker_tools(claim.role)
        ),
        max_budget=MAX_WORKER_BUDGET,
        max_timeout_seconds=MAX_WORKER_TIMEOUT_SECONDS,
        controller_version=claim.controller_version,
    )


def _payload(record: ProjectionRecord) -> dict[str, JsonValue]:
    document: JsonValue = json.loads(record.state_json)
    payload = document.get("payload") if type(document) is dict else None
    if type(payload) is not dict:
        raise ControllerExecutionError("EXECUTION_STATE_INVALID")
    return payload


def _text(payload: dict[str, JsonValue], field: str) -> str:
    value = payload.get(field)
    if type(value) is not str or not value:
        raise ControllerExecutionError("EXECUTION_STATE_INVALID")
    return value
