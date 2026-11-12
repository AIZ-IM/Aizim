from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from aizim.agents import AgentBackend, AgentRequest, AgentResult, BackendIdentity, FakeAgentBackend
from aizim.agents.fake_backend import FakeToolAction
from aizim.domain.serialization import JsonValue
from aizim.gateway import connect_gateway
from aizim.gateway.capabilities import GatewaySuccess, GatewayTool
from aizim.gateway.transport import GatewayTransport
from aizim.gateway.transport_frames import GatewayResult

from .benchmark_ladder import BenchmarkLadder, LadderTarget
from .records import ResearchError, ResearchStore, object_value, text, texts

_WORKER: ContextVar[str] = ContextVar("benchmark_worker")
_UNCHARGED = frozenset({
    "REQUEST_RATE_EXCEEDED", "RUN_BUDGET_EXHAUSTED", "WORKER_BUDGET_EXHAUSTED",
    "MISSING_TOKEN", "UNKNOWN_TOKEN",
})


@dataclass(slots=True)
class CallCounts:
    requests: int = 0
    charged: int = 0
    unknown: int = 0


@dataclass(slots=True)
class CallMeter:
    state: ResearchStore
    run_id: str
    workers: dict[str, CallCounts] = field(default_factory=dict)

    async def connect(self, socket: Path, session_id: str) -> MeasuredChannel:
        worker = _WORKER.get()
        counts = self.workers.setdefault(worker, CallCounts())
        channel = await connect_gateway(socket, session_id)
        return MeasuredChannel(channel, counts, self)

    def charged(self, result: GatewayResult) -> bool | None:
        if isinstance(result, GatewaySuccess):
            return True
        if result.error.event_id is None:
            return None
        for record in self.state.query_events(self.run_id):
            event = record.envelope
            if event.event_id == result.error.event_id:
                return event.payload.get("reason_code") not in _UNCHARGED
        return None


class MeasuredChannel:
    def __init__(self, inner: GatewayTransport, counts: CallCounts, meter: CallMeter) -> None:
        self.inner, self.counts, self.meter = inner, counts, meter
        self.role, self.tools = inner.role, inner.tools

    async def call(
        self, operation: GatewayTool | str, payload: dict[str, JsonValue],
    ) -> GatewayResult:
        self.counts.requests += 1
        try:
            result = await self.inner.call(operation, payload)
        except BaseException:
            # A lost/cancelled reply cannot establish whether the gateway charged it.
            self.counts.unknown += 1
            raise
        charged = self.meter.charged(result)
        if charged is None:
            self.counts.unknown += 1
        elif charged:
            self.counts.charged += 1
        return result

    async def aclose(self) -> None:
        await self.inner.aclose()


class MeasuredBackend:
    def __init__(self, inner: AgentBackend, meter: CallMeter) -> None:
        self.inner, self.meter = inner, meter

    @property
    def identity(self) -> BackendIdentity:
        return self.inner.identity

    async def run(self, request: AgentRequest) -> AgentResult:
        self.meter.workers.setdefault(request.worker_id, CallCounts())
        token = _WORKER.set(request.worker_id)
        try:
            return await self.inner.run(request)
        finally:
            _WORKER.reset(token)


class ScriptedLadderBackend:
    """Script normal worker tool calls; only the host can verify and publish."""

    def __init__(
        self, ladder: BenchmarkLadder, target: LadderTarget, meter: CallMeter,
    ) -> None:
        self.ladder, self.target, self.meter = ladder, target, meter

    @property
    def identity(self) -> BackendIdentity:
        return BackendIdentity("fake", "research-ladder-script-v1", None)

    async def run(self, request: AgentRequest) -> AgentResult:
        # LeanExecutor appends this exact target contract after the advisory context.
        contract = object_value(json.loads(request.prompt.rsplit("\n", 1)[-1]))
        target = self.target
        if contract["statement"] != target.statement:
            raise ResearchError("BENCHMARK_TARGET_MISMATCH")
        facts = contract["available_facts"]
        if type(facts) is not list:
            raise ResearchError("BENCHMARK_DEPENDENCY_MISSING")
        names: dict[str, str] = {}
        for dependency in target.depends_on:
            statement = self.ladder.target(dependency).statement
            matches = [
                object_value(fact) for fact in facts if object_value(fact)["type"] == statement
            ]
            if len(matches) != 1:
                raise ResearchError("BENCHMARK_DEPENDENCY_MISSING")
            names[dependency] = text(matches[0]["name"])
        proof = target.proof.format_map(names)
        imports: list[JsonValue] = list(texts(contract["imports"]))
        document = request.context["document_id"]
        line = len(imports) + 3
        actions = (
            FakeToolAction(GatewayTool.LEAN_GOAL, {"document_id": document, "line": line}),
            FakeToolAction(GatewayTool.LEAN_MULTI_ATTEMPT, {
                "document_id": document, "line": line, "snippets": [proof],
            }),
            FakeToolAction(GatewayTool.DOCUMENT_APPLY, {
                "document_id": document, "accepted": proof,
            }),
            FakeToolAction(GatewayTool.CONTRIBUTION_SUBMIT, {
                "document_id": document,
                "contribution_id": request.context["contribution_id"],
                "candidate_name": request.context["candidate_name"],
                "complete_type": target.statement,
                "imports": imports,
                "dependencies": list(names.values()),
                "assumptions": [],
                "evidence_links": [],
            }),
            FakeToolAction(GatewayTool.KNOWLEDGE_READ, {
                "after_knowledge_epoch": request.context["knowledge_epoch"], "wait": True,
            }),
        )
        result = await FakeAgentBackend(actions, connect=self.meter.connect).run(request)
        # Scripted actions consume no model tokens. No provider measurement is invented.
        return result
