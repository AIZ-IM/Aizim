from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from aizim.agents import AgentBackend, AgentRequest, AgentResult, BackendIdentity
from aizim.agents.usage import checked_usage
from aizim.domain import AgentRole
from aizim.domain.serialization import JsonValue
from aizim.gateway import GatewayLimits
from aizim.knowledge.target_binding import validate_published_target
from aizim.orchestration.agent_workspace import ResearchWorkspaceBackend
from aizim.orchestration.run_identity import candidate_name
from aizim.orchestration.worker import WorkerExecutionError
from aizim.orchestration.worker_authority import WorkerBackend, WorkerDirective
from aizim.orchestration.worker_gateway import CONTROLLER_WORKER_TOOLS
from aizim.state import EventEnvelope, StateService
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

# Gateway calls one attempt may make. Each attempt runs under its own worker identity.
ATTEMPT_CALL_BUDGET = 400
# A session runs many attempts; the per-attempt budget and the rate limit bound its spend.
RESEARCH_GATEWAY_LIMITS = GatewayLimits(200, 60.0, 1_000_000, 10_000)
# Stable reason codes may be reported; other exception text can carry paths or secrets.
_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
# A refusal caused by the attempt spending its own allowance, not by a fault.
_SPENT_BUDGET = "WORKER_BUDGET_EXHAUSTED"
# Consecutive infrastructure failures after which a target waits for the operator.
INFRASTRUCTURE_PAUSE_LIMIT = 3
# Publication grace scales with the target timeout, capped to bound shutdown latency.
PUBLICATION_WAIT_SECONDS = 900.0
# Worker start-up and teardown time beyond the worker's own timeout.
ROUND_OVERHEAD_SECONDS = 120.0
RUN_INFRASTRUCTURE_FAILURE_LIMIT = 3


@dataclass(frozen=True, slots=True)
class AttemptResult:
    feedback: str
    declaration_ids: tuple[str, ...] = ()
    usage: dict[str, int] | None = None
    controller_usage: dict[str, int] | None = None
    model: str | None = None
    # The round could not run as a proof attempt, so it is not a failed proof.
    infrastructure_failure: bool = False


def reason_code(error: BaseException) -> str | None:
    message = error.args[0] if len(error.args) == 1 else None
    if type(message) is str and _REASON_CODE.fullmatch(message) is not None:
        return message
    return None


class AttemptHost(Protocol):
    async def run(self, directive: WorkerDirective, backend: WorkerBackend, /) -> object: ...


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
        host: AttemptHost,
        backend: AgentBackend,
    ) -> None:
        self.state, self.project, self.host = state, project, host
        self.backend = backend

    async def __call__(self, task, attempt, context: str) -> AttemptResult:
        run_id, worker_id = text(attempt["run_id"]), text(attempt["worker_id"])
        name = candidate_name(run_id, worker_id)
        statement = text(task["statement"])
        imports = texts(task["imports"])
        call_budget = integer(attempt.get("call_budget", task.get("call_budget", 400)), minimum=1)
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
                    "gateway_call_budget": call_budget,
                    "contract": (
                        "Prove the supplied immutable statement through the Lean gateway. "
                        "Preserve its exact assumptions, quantifiers, complete_type, and "
                        "candidate_name. Do not replace the target with an easier proposition. "
                        "Submit the supplied declaration; a prose verdict does not complete it."
                    ),
                },
                ensure_ascii=False,
            )
        )
        timeout = integer(task["timeout_seconds"], minimum=1)
        tracked = TrackingBackend(self.backend)
        backend = ResearchWorkspaceBackend(tracked, self.project, instruction)
        directive = WorkerDirective(
            text(attempt["id"]),
            worker_id,
            AgentRole.PROOF_EXPLORER,
            source,
            call_budget,
            float(timeout),
            CONTROLLER_WORKER_TOOLS,
        )
        stopped: str | None = None
        try:
            await self.host.run(directive, backend)
        except WorkerExecutionError as error:
            # The worker ended without an accepted submission. Its report still guides the
            # next round, and a contribution made before it stopped may still publish.
            stopped = str(error)
        agent = tracked.result
        usage = None if agent is None else checked_usage(agent.usage)
        model = None if agent is None else agent.model
        # Submission and publication are distinct. Wait for this worker's own contribution.
        deadline = asyncio.get_running_loop().time() + min(timeout, PUBLICATION_WAIT_SECONDS)
        while True:
            accepted = verified_for_worker(
                self.state, run_id, worker_id, task, attempt, self.project
            )
            if accepted:
                return AttemptResult(
                    "Lean verified the target.", tuple(accepted), usage, model=model
                )
            events = [record.envelope for record in self.state.query_events(run_id)]
            contributions = {
                event.payload.get("contribution_id")
                for event in events
                if event.event_type == "ContributionSubmitted"
                and event.payload.get("worker_id") == worker_id
                and event.payload.get("candidate_name") == name
                and event.payload.get("complete_type") == statement
            }
            rejected = [
                str(event.payload.get("reason_code"))
                for event in events
                if event.event_type == "PromotionFailed"
                and event.payload.get("contribution_id") in contributions
            ]
            refusals = _gateway_outcomes(events, worker_id, "CapabilityDenied")
            infrastructure = False
            if rejected:
                headline = "Lean rejected the submission: " + "; ".join(rejected) + "."
            elif not contributions:
                infrastructure = stopped == "BACKEND_FAILED" and _SPENT_BUDGET not in refusals
                headline = (
                    "The submission did not match the target declaration."
                    if stopped is None
                    else "Worker did not submit a proof."
                )
            elif asyncio.get_running_loop().time() >= deadline:
                headline = "Publication did not finish within the round deadline."
            else:
                await asyncio.sleep(0.05)
                continue
            feedback = _round_report(headline, stopped, events, worker_id, agent)
            return AttemptResult(
                feedback, (), usage, model=model, infrastructure_failure=infrastructure
            )


def _gateway_outcomes(
    events: list[EventEnvelope], worker_id: str, event_type: str
) -> Counter[str]:
    return Counter(
        str(event.payload.get("reason_code"))
        for event in events
        if event.event_type == event_type and event.payload.get("worker_id") == worker_id
        and not (
            event_type == "CapabilityDenied"
            and event.payload.get("reason_code") == "REQUEST_RATE_EXCEEDED"
        )
    )


def _round_report(
    headline: str,
    stopped: str | None,
    events: list[EventEnvelope],
    worker_id: str,
    agent: AgentResult | None,
) -> str:
    """Summarize a closed round for the operator and the next attempt."""
    lines = [headline]
    if stopped is not None:
        lines.append(f"Worker stopped: {stopped}.")
    for label, event_type in (
        ("Gateway errors", "GatewayCallFailed"),
        ("Gateway refusals", "CapabilityDenied"),
    ):
        counts = _gateway_outcomes(events, worker_id, event_type)
        if counts:
            listed = ", ".join(f"{code} ({count})" for code, count in sorted(counts.items()))
            lines.append(f"{label}: {listed}.")
    if agent is not None and agent.summary.strip():
        lines.append("Agent report:\n" + agent.summary.strip())
    return "\n".join(lines)


def verified_for_worker(
    store: ResearchStore,
    run_id: str,
    worker_id: str,
    task: dict[str, JsonValue],
    attempt: dict[str, JsonValue],
    project_root: Path | None,
) -> list[str]:
    # Revalidate the stored source as well: older publications predate target binding.
    # Missing historical context remains readable but cannot newly settle a target.
    if task["target_hash"] != attempt["target_hash"]:
        raise ResearchError("TARGET_CHANGED_DURING_ROUND")
    if project_root is None:
        return []
    candidate = candidate_name(run_id, worker_id)
    contributed = {
        event.envelope.payload.get("contribution_id")
        for event in store.query_events(run_id)
        if event.envelope.event_type == "ContributionSubmitted"
        and event.envelope.payload.get("worker_id") == worker_id
        and event.envelope.payload.get("candidate_name") == candidate
        and event.envelope.payload.get("complete_type") == task["statement"]
        and (binding := validate_published_target(
            store, project_root, run_id, worker_id, event.envelope.payload
        )) is not None
        and binding.target_hash == attempt["target_hash"]
    }
    return [
        text(thaw_payload(event.envelope.payload)["declaration_id"])
        for event in store.query_events(run_id)
        if event.envelope.event_type == "DeclarationPublished"
        and event.envelope.payload.get("contribution_id") in contributed
    ]


def publication_pending(
    store: ResearchStore, run_id: str, worker_id: str, task: dict[str, JsonValue]
) -> bool:
    """Return whether this worker submitted the target and promotion has not settled it."""
    name = candidate_name(run_id, worker_id)
    events = [record.envelope for record in store.query_events(run_id)]
    submitted = {
        event.payload.get("contribution_id")
        for event in events
        if event.event_type == "ContributionSubmitted"
        and event.payload.get("worker_id") == worker_id
        and event.payload.get("candidate_name") == name
        and event.payload.get("complete_type") == task["statement"]
    }
    settled = {
        event.payload.get("contribution_id")
        for event in events
        if event.event_type in {"DeclarationPublished", "PromotionFailed"}
    }
    # A rebased contribution is superseded by its resubmission, which settles on its own.
    superseded = {
        event.payload.get("source_contribution_id")
        for event in events
        if event.event_type == "ContributionRebased"
    }
    return bool(submitted - settled - superseded)


class ResearchEngine:
    def __init__(
        self,
        store: ResearchStore,
        execute: Executor,
        run_id: str,
        model: str,
        *,
        workers: int = 2,
        context_search: Callable[[str], list[dict[str, JsonValue]]] | None = None,
        project_root: Path | None = None,
    ) -> None:
        if not 1 <= workers <= 64:
            raise ResearchError("INVALID_WORKER_LIMIT")
        self.store, self.execute, self.run_id, self.model = store, execute, run_id, model
        self.workers, self.context_search = workers, context_search
        self.project_root = project_root or (
            store.socket_path.parents[2] if isinstance(store, StateService) else None
        )
        self.stop = asyncio.Event()
        self.active: set[asyncio.Task[None]] = set()
        self.reason = ""
        self.infrastructure_failures = 0

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
            recovering = attempt["status"] == "running" or task["status"] == "running"
            accounted = {
                **task,
                "rounds": sum(entry.get("charged", True) is True for entry in candidates),
                "attempt_sequence": max(integer(entry["round"]) for entry in candidates),
            }
            # Keep settlement discoverable if recovery itself stops between the two saves.
            if recovering and task["status"] != "verified":
                accounted["status"] = "running"
            if accounted != task:
                save(self.store, "task", text(task["task_id"]), accounted,
                     actor="research_supervisor")
            task = accounted
            if not recovering:
                continue
            accepted = verified_for_worker(
                self.store, text(attempt["run_id"]), text(attempt["worker_id"]), task, attempt,
                self.project_root,
            )
            self._finish(
                attempt,
                AttemptResult(
                    "Recovered after an interrupted run.",
                    tuple(accepted),
                    checked_usage(attempt.get("usage")),
                    checked_usage(attempt.get("controller_usage")),
                    None if attempt.get("model") is None else text(attempt["model"]),
                    infrastructure_failure=attempt.get("infrastructure_failure") is True,
                ),
                interrupted=not accepted and attempt.get("status") in {"running", "interrupted"},
            )
        self._reconcile()

    def _reconcile(self) -> bool:
        """Settle rounds whose submission published or failed after the round closed.

        Return whether a closed round still awaits its publication.
        """
        waiting = False
        for attempt in records(self.store, "attempt"):
            if attempt.get("awaiting_publication") is not True:
                continue
            task = require_task(self.store, text(attempt["task_id"]))
            run_id, worker_id = text(attempt["run_id"]), text(attempt["worker_id"])
            accepted = verified_for_worker(
                self.store, run_id, worker_id, task, attempt, self.project_root
            )
            if not accepted and publication_pending(self.store, run_id, worker_id, task):
                waiting = True
                continue
            settled = {
                key: value for key, value in attempt.items() if key != "awaiting_publication"
            }
            if accepted:
                settled |= {
                    "status": "verified", "verified_declarations": list(accepted),
                    "charged": True, "infrastructure_failure": False,
                }
            save(
                self.store, "attempt", text(attempt["id"]), settled,
                actor="research_supervisor", run_id=run_id,
            )
            if accepted and task["status"] != "verified":
                save(
                    self.store,
                    "task",
                    text(task["task_id"]),
                    {
                        **{
                            key: value
                            for key, value in task.items()
                            if key != "infrastructure_failures"
                        },
                        "status": "verified",
                        "feedback": "Lean verified a submission after its round closed.",
                        "failures": 0,
                        "rounds": sum(
                            entry.get("charged", True) is True
                            for entry in records(self.store, "attempt")
                            if entry["task_id"] == task["task_id"]
                        ),
                        "verified_declarations": list(accepted),
                        "paused": False,
                    },
                    actor="research_supervisor",
                    run_id=run_id,
                )
        return waiting

    async def run(self, *, watch: bool = False) -> None:
        self.recover()
        loop = asyncio.get_running_loop()
        # Closing the host cancels promotions, so an idle run first lets its submissions settle.
        settle_by: float | None = None
        try:
            while not self.stop.is_set():
                waiting = self._reconcile()
                while not self.reason and len(self.active) < self.workers:
                    ready = frontier(self.store)
                    if not ready:
                        break
                    # Sample other ready targets before retrying one with an outage.
                    ready.sort(key=lambda task: integer(task.get("infrastructure_failures", 0)))
                    attempt = self._begin(ready[0])
                    pending = asyncio.create_task(self._attempt(ready[0], attempt))
                    self.active.add(pending)
                if not self.active:
                    if self.reason:
                        break
                    if not watch:
                        grace = max(
                            (
                                min(integer(task["timeout_seconds"]), PUBLICATION_WAIT_SECONDS)
                                for task in records(self.store, "task")
                            ),
                            default=PUBLICATION_WAIT_SECONDS,
                        )
                        settle_by = settle_by or loop.time() + grace
                        if not waiting or loop.time() >= settle_by:
                            break
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self.stop.wait(), 0.25)
                    continue
                settle_by = None
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
        prior = [entry for entry in records(self.store, "attempt") if entry["task_id"] == task_id]
        round_number = max(
            integer(task.get("attempt_sequence", task["rounds"])),
            max((integer(entry["round"]) for entry in prior), default=0),
        ) + 1
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
            "inbox_ids": [],
            "call_budget": integer(task.get("call_budget", ATTEMPT_CALL_BUDGET), minimum=1),
            "charged": True,
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
            {**task, "status": "running", "rounds": integer(task["rounds"]) + 1,
             "attempt_sequence": round_number},
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
            # This bound only catches a hung round. It outlasts the worker, its start-up, and
            # the publication wait, so a slow publication is reported as one, not as an outage.
            timeout = integer(task["timeout_seconds"])
            bound = timeout + min(timeout, PUBLICATION_WAIT_SECONDS)
            result = await asyncio.wait_for(
                self.execute(task, attempt, context), timeout=bound + ROUND_OVERHEAD_SECONDS
            )
        except asyncio.CancelledError:
            self._finish(attempt, AttemptResult("Interrupted by the operator."), interrupted=True)
            raise
        except Exception as error:
            code = reason_code(error)
            detail = type(error).__name__ if code is None else f"{type(error).__name__}: {code}"
            result = AttemptResult(f"Round failed: {detail}.", infrastructure_failure=True)
        self._finish(attempt, result)
        settled = get(self.store, "attempt", text(attempt["id"]))
        if settled is not None and settled.get("infrastructure_failure") is True:
            self.infrastructure_failures += 1
            if self.infrastructure_failures >= RUN_INFRASTRUCTURE_FAILURE_LIMIT:
                self.reason = "infrastructure_unavailable"
        else:
            self.infrastructure_failures = 0

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

    @staticmethod
    def _settled_task(
        task: dict[str, JsonValue],
        result: AttemptResult,
        accepted: list[str],
        feedback: str,
        *,
        interrupted: bool,
    ) -> dict[str, JsonValue]:
        """Charge a closed round to the failure budget or to the infrastructure streak."""
        verified = bool(accepted)
        # An outage says nothing about the proof, so it spares the failure budget.
        infrastructure = result.infrastructure_failure and not verified and not interrupted
        streak = integer(task.get("infrastructure_failures", 0))
        streak = streak + 1 if infrastructure else streak if interrupted else 0
        failures = integer(task["failures"])
        failures = 0 if verified else failures if interrupted or infrastructure else failures + 1
        exhausted = integer(task["rounds"]) >= integer(task["max_rounds"]) or failures >= integer(
            task["max_failures"]
        )
        settled: dict[str, JsonValue] = {
            **{key: value for key, value in task.items() if key != "infrastructure_failures"},
            "status": "verified" if verified else "blocked" if exhausted else "pending",
            "feedback": feedback,
            "failures": failures,
            "verified_declarations": list(accepted),
            # A verified target cannot be resumed, so it must not stay paused.
            "paused": False if verified else task["paused"],
        }
        if streak:
            settled["infrastructure_failures"] = streak
        if streak >= INFRASTRUCTURE_PAUSE_LIMIT:
            settled["paused"] = True
            settled["feedback"] = (
                f"{feedback}\nPaused after {streak} consecutive infrastructure failures; "
                "check the worker environment, then resume the task."
            )
        return settled

    def _finish(
        self, attempt: dict[str, JsonValue], result: AttemptResult, *, interrupted: bool = False
    ) -> None:
        task_id, identity = text(attempt["task_id"]), text(attempt["id"])
        task = require_task(self.store, task_id)
        if task["target_hash"] != attempt["target_hash"]:
            raise ResearchError("TARGET_CHANGED_DURING_ROUND")
        accepted = verified_for_worker(
            self.store, text(attempt["run_id"]), text(attempt["worker_id"]), task, attempt,
            self.project_root,
        )
        if set(result.declaration_ids) - set(accepted):
            raise ResearchError("UNTRUSTED_VERIFICATION_RESULT")
        verified = bool(accepted)
        feedback = result.feedback[:8192] or "No result."
        usage: JsonValue = (
            None if result.usage is None else {key: value for key, value in result.usage.items()}
        )
        controller_usage: JsonValue = (
            None if result.controller_usage is None
            else {key: value for key, value in result.controller_usage.items()}
        )
        run_id, worker_id = text(attempt["run_id"]), text(attempt["worker_id"])
        settled: dict[str, JsonValue] = {
            **{key: value for key, value in attempt.items() if key != "awaiting_publication"},
            "status": "verified" if verified else "interrupted" if interrupted else "failed",
            "feedback": feedback,
            "verified_declarations": list(accepted),
            "usage": usage,
            "model": result.model or attempt["model"],
            **({"controller_usage": controller_usage} if "controller_usage" in attempt else {}),
            "infrastructure_failure": (
                result.infrastructure_failure and not verified and not interrupted
            ),
            "charged": verified or not (result.infrastructure_failure or interrupted),
            "finished_at": datetime.now(UTC).isoformat(),
        }
        if not verified and publication_pending(self.store, run_id, worker_id, task):
            # The submission may still publish; _reconcile settles this round when it does.
            settled["awaiting_publication"] = True
        save(self.store, "attempt", identity, settled, actor="research_supervisor", run_id=run_id)
        # Recompute from durable attempts: recovery after either save cannot double-charge
        # or double-refund an infrastructure failure or interrupted attempt.
        task["rounds"] = sum(
            entry.get("charged", True) is True
            for entry in records(self.store, "attempt") if entry["task_id"] == task_id
        )
        # A late publication may already have verified the target; a later round cannot undo it.
        settled_task = (
            task if not verified and task["status"] == "verified"
            else self._settled_task(task, result, accepted, feedback, interrupted=interrupted)
        )
        save(
            self.store, "task", task_id, settled_task,
            actor="research_supervisor", run_id=run_id,
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
