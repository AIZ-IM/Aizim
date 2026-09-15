from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from aizim.agents import AgentBackend, AgentRequest, AgentResult, BackendIdentity
from aizim.agents.codex_events import checked_usage
from aizim.domain import AgentRole
from aizim.domain.serialization import JsonValue
from aizim.orchestration.codex_worker import CodexWorkspaceBackend
from aizim.orchestration.controller_backend import (
    ControllerBackend,
    ControllerContext,
    DispatchDecision,
)
from aizim.orchestration.run_identity import candidate_name
from aizim.orchestration.worker_authority import WorkerDirective
from aizim.orchestration.worker_gateway import CONTROLLER_WORKER_TOOLS
from aizim.orchestration.worker_host import WorkerExecutionHost
from aizim.state import StateService
from aizim.state.event_payload import thaw_payload

from .records import (
    ResearchError,
    ResearchStore,
    frontier,
    get,
    integer,
    object_value,
    records,
    require_task,
    save,
    text,
    texts,
)
from .search import memory_search


@dataclass(frozen=True, slots=True)
class AttemptResult:
    feedback: str
    declaration_ids: tuple[str, ...] = ()
    usage: dict[str, int] | None = None
    controller_usage: dict[str, int] | None = None


class Executor(Protocol):
    async def __call__(
        self,
        task: dict[str, JsonValue],
        attempt: dict[str, JsonValue],
        context: str,
        /,
    ) -> AttemptResult: ...


def statement_source(name: str, statement: str, imports: list[str]) -> bytes:
    if ":=" in statement:
        raise ResearchError("STATEMENT_MUST_NOT_CONTAIN_A_PROOF")
    separator = "" if statement.startswith(("(", "{", "[")) else ": "
    return (
        "".join(f"import {module}\n" for module in imports)
        + f"\ntheorem {name} {separator}{statement} := by\n  sorry\n"
    ).encode()


class TrackingBackend:
    def __init__(self, inner: AgentBackend) -> None:
        self.inner = inner
        self.result: AgentResult | None = None

    @property
    def identity(self) -> BackendIdentity:
        return self.inner.identity

    async def run(self, request: AgentRequest) -> AgentResult:
        self.result = await self.inner.run(request)
        return self.result


class LeanExecutor:
    def __init__(
        self,
        state: StateService,
        project: Path,
        host: WorkerExecutionHost,
        backend: AgentBackend,
        model: str,
        controller_factory: Callable[[], ControllerBackend] | None = None,
    ) -> None:
        self.state, self.project, self.host = state, project, host
        self.backend, self.model, self.controller_factory = backend, model, controller_factory

    async def __call__(self, task, attempt, context: str) -> AttemptResult:
        run_id, worker_id = text(attempt["run_id"]), text(attempt["worker_id"])
        controller_usage: dict[str, int] | None = None

        def finish(
            feedback: str,
            declaration_ids: tuple[str, ...] = (),
            usage: dict[str, int] | None = None,
        ) -> AttemptResult:
            return AttemptResult(feedback, declaration_ids, usage, controller_usage)

        name = candidate_name(run_id, worker_id)
        statement = text(task["statement"])
        imports = texts(task["imports"])
        known: list[dict[str, JsonValue]] = []
        for dependency in texts(task["depends_on"]):
            parent = require_task(self.state, dependency)
            for declaration in texts(parent["verified_declarations"]):
                record = self.state.query_projection("verified_declarations", declaration)
                if record is None:
                    raise ResearchError("VERIFIED_DEPENDENCY_MISSING")
                payload = object_value(object_value(json.loads(record.state_json))["payload"])
                imports.append(text(payload["module"]))
                known.append(payload)
        imports = list(dict.fromkeys(imports))
        source = statement_source(name, statement, imports)
        instruction = (
            context
            + "\n"
            + json.dumps(
                {
                    "statement": statement,
                    "candidate_name": name,
                    "imports": imports,
                    "available_facts": known,
                    "initial_source": source.decode(),
                    "contract": (
                        "Prove the supplied statement through the Lean gateway. "
                        "Preserve its assumptions and quantifiers. Submit that declaration "
                        "with the supplied candidate_name and complete_type. "
                        "A prose verdict does not complete this task."
                    ),
                },
                ensure_ascii=False,
            )
        )
        budget, timeout = 12, integer(task["timeout_seconds"], minimum=1)
        if self.controller_factory is not None:
            controller = self.controller_factory()
            epoch = self.state.query_projection("epochs", "global")
            if epoch is None:
                raise ResearchError("PROJECT_EPOCH_MISSING")
            pair = object_value(json.loads(epoch.state_json))
            try:
                decision = await asyncio.wait_for(
                    controller.plan(
                        ControllerContext(
                            text(attempt["id"]),
                            integer(attempt["round"]),
                            (
                                "You are the planning controller. Return a bounded dispatch, "
                                "blocked, "
                                "or reject decision for the assigned worker. The listed operations "
                                "belong to that worker and will be available after dispatch. "
                                "Plan an investigation of the formal target. Delegate its Lean "
                                "tool calls to the worker. A missing tool in your session is not a "
                                "missing worker capability. Preserve the target and assumptions. "
                                f"Dispatch only to worker {worker_id}. Worker context follows:\n"
                                + instruction
                            ),
                            worker_id,
                            AgentRole.PROOF_EXPLORER,
                            self.project.name,
                            text(pair["base_epoch"]),
                            integer(pair["knowledge_epoch"]),
                            tuple(tool.value for tool in CONTROLLER_WORKER_TOOLS),
                            budget,
                            float(timeout),
                            1,
                        )
                    ),
                    timeout=min(timeout, 120),
                )
            except Exception as error:
                controller_usage = checked_usage(getattr(controller, "last_usage", None))
                code = getattr(error, "code", type(error).__name__)
                return finish(f"Controller request failed: {code}.")
            controller_usage = checked_usage(getattr(controller, "last_usage", None))
            if not isinstance(decision, DispatchDecision) or decision.worker_id != worker_id:
                reason = getattr(decision, "reason_code", "WORKER_MISMATCH")
                return finish(f"Controller did not dispatch this target: {reason}.")
            if not 1 <= decision.budget <= budget or not 0 < decision.timeout_seconds <= timeout:
                raise ResearchError("CONTROLLER_LIMIT_EXCEEDED")
            instruction += "\nController guidance:\n" + decision.instruction
            budget, timeout = decision.budget, min(timeout, int(decision.timeout_seconds))
        tracked = TrackingBackend(self.backend)
        backend = CodexWorkspaceBackend(tracked, self.project, self.model, instruction)
        directive = WorkerDirective(
            text(attempt["id"]),
            worker_id,
            AgentRole.PROOF_EXPLORER,
            source,
            budget,
            float(timeout),
            CONTROLLER_WORKER_TOOLS,
        )
        await self.host.run(directive, backend)
        # Submission and publication are distinct. Wait for this worker's own contribution.
        deadline = asyncio.get_running_loop().time() + min(timeout, 180)
        while True:
            accepted = verified_for_worker(self.state, run_id, worker_id)
            if accepted:
                return finish(
                    "Lean verified the target.",
                    tuple(accepted),
                    None if tracked.result is None else tracked.result.usage,
                )
            failures = [
                record.envelope
                for record in self.state.query_events(run_id)
                if record.envelope.event_type == "PromotionFailed"
            ]
            contributions = {
                record.envelope.payload.get("contribution_id")
                for record in self.state.query_events(run_id)
                if record.envelope.event_type == "ContributionSubmitted"
                and record.envelope.payload.get("worker_id") == worker_id
            }
            failed = [
                event for event in failures if event.payload.get("contribution_id") in contributions
            ]
            if failed:
                feedback = "; ".join(str(event.payload.get("reason_code")) for event in failed)
                return finish(
                    feedback, usage=None if tracked.result is None else tracked.result.usage
                )
            if tracked.result is None or tracked.result.status != "submitted":
                return finish(
                    "Worker did not submit a proof."
                    if tracked.result is None
                    else tracked.result.summary,
                    usage=None if tracked.result is None else tracked.result.usage,
                )
            if asyncio.get_running_loop().time() >= deadline:
                return finish(
                    "Publication did not finish within the round deadline.",
                    usage=tracked.result.usage,
                )
            await asyncio.sleep(0.05)


def verified_for_worker(store: ResearchStore, run_id: str, worker_id: str) -> list[str]:
    contributed = {
        event.envelope.payload.get("contribution_id")
        for event in store.query_events(run_id)
        if event.envelope.event_type == "ContributionSubmitted"
        and event.envelope.payload.get("worker_id") == worker_id
    }
    return [
        text(thaw_payload(event.envelope.payload)["declaration_id"])
        for event in store.query_events(run_id)
        if event.envelope.event_type == "DeclarationPublished"
        and event.envelope.payload.get("contribution_id") in contributed
    ]


class ResearchEngine:
    def __init__(
        self,
        store: ResearchStore,
        execute: Executor,
        run_id: str,
        model: str,
        *,
        workers: int = 2,
        controller_model: str = "",
        validate_control: Callable[[], None] | None = None,
        context_search: Callable[[str], list[dict[str, JsonValue]]] | None = None,
    ) -> None:
        if not 1 <= workers <= 64:
            raise ResearchError("INVALID_WORKER_LIMIT")
        self.store, self.execute, self.run_id, self.model = store, execute, run_id, model
        self.controller_model = controller_model
        self.validate_control = validate_control
        self.workers, self.context_search = workers, context_search
        self.stop = asyncio.Event()
        self.active: set[asyncio.Task[None]] = set()

    def request_stop(self) -> None:
        self.stop.set()
        for pending in self.active:
            pending.cancel()

    def recover(self) -> None:
        attempts = records(self.store, "attempt")
        for task in records(self.store, "task"):
            candidates = [entry for entry in attempts if entry["task_id"] == task["task_id"]]
            if not candidates:
                continue
            attempt = max(candidates, key=lambda entry: integer(entry["round"]))
            if attempt["status"] != "running" and task["status"] != "running":
                continue
            task["rounds"] = max(integer(task["rounds"]), integer(attempt["round"]))
            save(self.store, "task", text(task["task_id"]), task, actor="research_supervisor")
            accepted = verified_for_worker(
                self.store, text(attempt["run_id"]), text(attempt["worker_id"])
            )
            self._finish(
                attempt,
                AttemptResult(
                    "Recovered after an interrupted run.",
                    tuple(accepted),
                    checked_usage(attempt.get("usage")),
                    checked_usage(attempt.get("controller_usage")),
                ),
                interrupted=not accepted,
            )

    async def run(self, *, watch: bool = False) -> None:
        self.recover()
        try:
            while not self.stop.is_set():
                if self.validate_control is not None:
                    self.validate_control()
                while len(self.active) < self.workers:
                    ready = frontier(self.store)
                    if not ready:
                        break
                    attempt = self._begin(ready[0])
                    pending = asyncio.create_task(self._attempt(ready[0], attempt))
                    self.active.add(pending)
                if not self.active:
                    if not watch:
                        break
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self.stop.wait(), 0.25)
                    continue
                done, _ = await asyncio.wait(self.active, return_when=asyncio.FIRST_COMPLETED)
                self.active.difference_update(done)
                for pending in done:
                    await pending
        finally:
            for pending in self.active:
                pending.cancel()
            await asyncio.gather(*self.active, return_exceptions=True)
            self.active.clear()

    def _begin(self, task: dict[str, JsonValue]) -> dict[str, JsonValue]:
        task_id = text(task["task_id"])
        identity = "attempt-" + secrets.token_hex(12)
        round_number = integer(task["rounds"]) + 1
        attempt: dict[str, JsonValue] = {
            "id": identity,
            "task_id": task_id,
            "worker_id": "worker-" + secrets.token_hex(12),
            "run_id": self.run_id,
            "target_hash": task["target_hash"],
            "round": round_number,
            "status": "running",
            "feedback": "",
            "verified_declarations": [],
            "usage": None,
            "model": self.model,
            "controller_model": self.controller_model,
            "controller_usage": None,
            "inbox_ids": [],
        }
        inbox = [
            entry for entry in records(self.store, "inbox") if entry["task_id"] in {"", task_id}
        ]
        attempt["inbox_ids"] = [entry["id"] for entry in inbox]
        save(
            self.store,
            "attempt",
            identity,
            attempt,
            actor="research_supervisor",
            run_id=self.run_id,
        )
        save(
            self.store,
            "task",
            task_id,
            {**task, "status": "running", "rounds": round_number},
            actor="research_supervisor",
            run_id=self.run_id,
        )
        for entry in inbox:
            consumed = texts(entry["consumed_by"])
            entry["consumed_by"] = [*consumed, identity]
            save(
                self.store,
                "inbox",
                text(entry["id"]),
                entry,
                actor="research_supervisor",
                run_id=self.run_id,
            )
        return attempt

    async def _attempt(self, task: dict[str, JsonValue], attempt: dict[str, JsonValue]) -> None:
        try:
            context = await self._context(task, attempt)
            result = await asyncio.wait_for(
                self.execute(task, attempt, context), timeout=integer(task["timeout_seconds"]) + 180
            )
        except asyncio.CancelledError:
            self._finish(attempt, AttemptResult("Interrupted by the operator."), interrupted=True)
            raise
        except Exception as error:
            result = AttemptResult(f"Round failed: {type(error).__name__}.")
        self._finish(attempt, result)

    async def _context(self, task: dict[str, JsonValue], attempt: dict[str, JsonValue]) -> str:
        identity = text(task["task_id"])
        previous = [
            entry
            for entry in records(self.store, "memory")
            if entry["task_id"] == identity and entry["origin"] == "worker"
        ][-5:]
        memories = memory_search(self.store, text(task["title"]), task_id=identity)
        inbox = [get(self.store, "inbox", item) for item in texts(attempt["inbox_ids"])]
        hits = (
            []
            if self.context_search is None
            else await asyncio.to_thread(self.context_search, text(task["statement"]))
        )
        return json.dumps(
            {
                "task": task,
                "recent_rounds": previous,
                "memory": memories,
                "operator_guidance": inbox,
                "local_lemmas": hits,
            },
            ensure_ascii=False,
        )

    def _finish(
        self, attempt: dict[str, JsonValue], result: AttemptResult, *, interrupted: bool = False
    ) -> None:
        task_id, identity = text(attempt["task_id"]), text(attempt["id"])
        task = require_task(self.store, task_id)
        if task["target_hash"] != attempt["target_hash"]:
            raise ResearchError("TARGET_CHANGED_DURING_ROUND")
        accepted = verified_for_worker(
            self.store, text(attempt["run_id"]), text(attempt["worker_id"])
        )
        if set(result.declaration_ids) - set(accepted):
            raise ResearchError("UNTRUSTED_VERIFICATION_RESULT")
        verified = bool(accepted)
        feedback = result.feedback[:8192] or "No result."
        usage: JsonValue = (
            None if result.usage is None else {key: value for key, value in result.usage.items()}
        )
        save(
            self.store,
            "attempt",
            identity,
            {
                **attempt,
                "status": "verified" if verified else "interrupted" if interrupted else "failed",
                "feedback": feedback,
                "verified_declarations": list(accepted),
                "usage": usage,
                "controller_usage": (
                    None
                    if result.controller_usage is None
                    else {key: value for key, value in result.controller_usage.items()}
                ),
            },
            actor="research_supervisor",
            run_id=text(attempt["run_id"]),
        )
        failures = 0 if verified else integer(task["failures"]) + (0 if interrupted else 1)
        exhausted = integer(task["rounds"]) >= integer(task["max_rounds"]) or failures >= integer(
            task["max_failures"]
        )
        save(
            self.store,
            "task",
            task_id,
            {
                **task,
                "status": "verified" if verified else "blocked" if exhausted else "pending",
                "feedback": feedback,
                "failures": failures,
                "verified_declarations": list(accepted),
            },
            actor="research_supervisor",
            run_id=text(attempt["run_id"]),
        )
        memory_id = "round-" + identity.removeprefix("attempt-")
        if get(self.store, "memory", memory_id) is None:
            save(
                self.store,
                "memory",
                memory_id,
                {
                    "id": memory_id,
                    "task_id": task_id,
                    "author": text(attempt["worker_id"]),
                    "kind": "summary",
                    "body": feedback,
                    "source_refs": list(accepted),
                    "origin": "worker",
                    "worker_id": "",
                    "verified": False,
                },
                actor="research_supervisor",
                run_id=text(attempt["run_id"]),
            )
