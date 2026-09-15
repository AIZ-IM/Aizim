from __future__ import annotations

import asyncio  # noqa: ANYIO_OK - composes the existing asyncio worker host
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from aizim.agents import AgentBackend
from aizim.domain import canonical_json, sha256_bytes, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.knowledge import ArtifactStore
from aizim.state import AppendEventCommand, ProjectionRecord, StateService

from .codex_worker import CodexWorkspaceBackend
from .controller_backend import (
    CONTROLLER_PLAN_TIMEOUT_SECONDS,
    MAX_CONTROLLER_INSTRUCTION_BYTES,
    MAX_WORKER_BUDGET,
    MAX_WORKER_TIMEOUT_SECONDS,
    BlockedDecision,
    ControllerBackend,
    ControllerBackendError,
    ControllerContext,
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

type TrustedIds = tuple[str, str]


async def unavailable_worker_preflight() -> None:
    raise ControllerExecutionError("WORKER_PREFLIGHT_UNAVAILABLE")


@dataclass(frozen=True, slots=True)
class ControllerDispatcherDependencies:
    state: StateService
    project: Path
    worker_model: str
    governor: ResourceGovernor
    worker_backend: AgentBackend
    controller_backend: ControllerBackend


@dataclass(frozen=True, slots=True)
class ControllerRun:
    version: int
    session_id: str
    project_id: str
    base_epoch: str
    knowledge_epoch: int


@dataclass(frozen=True, slots=True)
class PreparedDispatch:
    claim: ClaimedAssignment
    directive_id: str
    context: ControllerContext


class ControllerDispatcher:
    def __init__(self, dependencies: ControllerDispatcherDependencies) -> None:
        self._dependencies = dependencies

    def _fail(self, claim: ClaimedAssignment, code: str) -> None:
        fail_assignment(self._dependencies.state, claim, code)

    async def plan_and_execute(
        self,
        prepared: PreparedDispatch,
        entered: asyncio.Event,
    ) -> None:
        try:
            entered.set()
            decision = await asyncio.wait_for(
                self._dependencies.controller_backend.plan(prepared.context),
                CONTROLLER_PLAN_TIMEOUT_SECONDS,
            )
            match decision:
                case BlockedDecision():
                    self._fail(prepared.claim, "CONTROLLER_BLOCKED")
                case RejectDecision():
                    self._fail(prepared.claim, "CONTROLLER_REJECTED")
                case DispatchDecision() as dispatch:
                    await self._dispatch(prepared, dispatch)
                case unreachable:
                    assert_never(unreachable)
        except TimeoutError:
            self._fail(prepared.claim, "CONTROLLER_TIMEOUT")
        except ControllerBackendError:
            self._fail(prepared.claim, "CONTROLLER_DECISION_INVALID")
        except asyncio.CancelledError:
            interrupt_assignment(self._dependencies.state, prepared.claim, "OPERATOR_SIGNAL")
            raise

    async def _dispatch(
        self,
        prepared: PreparedDispatch,
        decision: DispatchDecision,
    ) -> None:
        claim, context = prepared.claim, prepared.context
        if not _valid_dispatch(decision, context):
            self._fail(claim, "CONTROLLER_DECISION_INVALID")
            return
        operations = controller_worker_tools(claim.role)
        if not operations:
            self._fail(claim, "WORKER_ROLE_UNSUPPORTED")
            return
        if not claim.task.startswith("prove ") or not claim.task.removeprefix("prove ").strip():
            self._fail(claim, "FORMAL_TARGET_REQUIRED")
            return
        from aizim.research.engine import statement_source

        from .run_identity import candidate_name

        execution_run_id = f"task-{claim.execution_id}"
        name = candidate_name(execution_run_id, claim.worker_id)
        initial_source = statement_source(name, claim.task.removeprefix("prove ").strip(), ["Std"])
        directive = WorkerDirective(
            directive_id=prepared.directive_id,
            worker_id=claim.worker_id,
            role=claim.role,
            initial_source=initial_source,
            budget=decision.budget,
            timeout_seconds=decision.timeout_seconds,
            operations=operations,
        )
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
            self._fail(claim, "WORKER_FAILED")
            return
        complete_assignment(self._dependencies.state, claim, sha256_json(cursor))


def prepare_next(
    state: StateService,
    run: ControllerRun,
    ids: TrustedIds,
) -> PreparedDispatch | None:
    configured = state.query_projection("controller", "primary")
    if configured is None or configured.version != run.version:
        raise ControllerExecutionError("CONTROLLER_VERSION_STALE")
    for worker in sorted(state.projections("worker_roster"), key=lambda item: item.entity_id):
        assignment = state.query_projection("worker_assignments", worker.entity_id)
        if assignment is None:
            continue
        assignment_id = _text(_payload(assignment), "assignment_id")
        if state.query_projection("worker_executions", assignment_id) is not None:
            continue
        execution_id, directive_id = ids
        claim = claim_assignment(
            state,
            worker_id=worker.entity_id,
            controller_session_id=run.session_id,
            controller_version=run.version,
            execution_id=execution_id,
        )
        return PreparedDispatch(
            claim,
            directive_id,
            ControllerContext(
                assignment_id=claim.assignment_id,
                task_version=claim.task_version,
                task=claim.task,
                worker_id=claim.worker_id,
                role=claim.role,
                project_id=run.project_id,
                base_epoch=run.base_epoch,
                knowledge_epoch=run.knowledge_epoch,
                allowed_operations=tuple(
                    operation.value for operation in controller_worker_tools(claim.role)
                ),
                max_budget=MAX_WORKER_BUDGET,
                max_timeout_seconds=MAX_WORKER_TIMEOUT_SECONDS,
                controller_version=claim.controller_version,
            ),
        )
    return None


def _valid_dispatch(decision: DispatchDecision, context: ControllerContext) -> bool:
    try:
        instruction_size = len(decision.instruction.encode())
    except (AttributeError, UnicodeEncodeError):
        return False
    return (
        decision.worker_id == context.worker_id
        and 0 < instruction_size <= MAX_CONTROLLER_INSTRUCTION_BYTES
        and type(decision.budget) is int
        and 1 <= decision.budget <= context.max_budget
        and type(decision.timeout_seconds) in {int, float}
        and math.isfinite(decision.timeout_seconds)
        and 0 < decision.timeout_seconds <= context.max_timeout_seconds
    )


def record_controller_stop(state: StateService, session_id: str, reason_code: str) -> None:
    payload: dict[str, JsonValue] = {
        "controller_id": "primary",
        "controller_session_id": session_id,
        "reason_code": reason_code,
    }
    state.append_event(AppendEventCommand("ControllerStopped", "primary", None, None, payload))


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
