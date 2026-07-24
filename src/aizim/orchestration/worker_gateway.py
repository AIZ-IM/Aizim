from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Final

from aizim.domain import AgentRole, sha256_bytes
from aizim.domain.serialization import JsonValue
from aizim.gateway import AuthorizedCall, GatewayTool, advertised_tools
from aizim.knowledge import (
    ArtifactStore,
    ContributionDraft,
    ContributionService,
    KnowledgeReader,
    PublishedKnowledgeDelta,
    SnapshotPayload,
)
from aizim.lean import DocumentBroker, SharedLeanRuntime
from aizim.lean.runtime_gateway import multi_attempt as runtime_multi_attempt
from aizim.state import StateService

from .knowledge_stream import KnowledgeStream

type GatewayTarget = Callable[[AuthorizedCall], Awaitable[JsonValue]]

CONTROLLER_WORKER_TOOLS: Final = (
    GatewayTool.PROJECT_READ,
    GatewayTool.LEAN_GOAL,
    GatewayTool.LEAN_MULTI_ATTEMPT,
    GatewayTool.LEAN_DIAGNOSTICS,
    GatewayTool.DOCUMENT_APPLY,
    GatewayTool.CONTRIBUTION_SUBMIT,
    GatewayTool.KNOWLEDGE_READ,
)
_WORKER_GATEWAY_TARGETS: Final = frozenset(
    {
        GatewayTool.LEAN_GOAL,
        GatewayTool.LEAN_MULTI_ATTEMPT,
        GatewayTool.LEAN_DIAGNOSTICS,
        GatewayTool.DOCUMENT_APPLY,
        GatewayTool.CONTRIBUTION_SUBMIT,
        GatewayTool.KNOWLEDGE_READ,
    }
)


class WorkerGatewayError(RuntimeError):
    pass


class WorkerGatewayActions:
    def __init__(
        self,
        state: StateService,
        broker: DocumentBroker,
        runtime: SharedLeanRuntime,
        knowledge: KnowledgeStream,
        artifacts: ArtifactStore,
        environment_fingerprint: str,
        on_submission: Callable[[], None],
    ) -> None:
        self._state, self._broker, self._runtime = state, broker, runtime
        self._knowledge, self._artifacts = knowledge, artifacts
        self._environment, self._on_submission = environment_fingerprint, on_submission
        self._accepted: dict[tuple[str, str, str, str], set[str]] = {}

    def targets(self) -> Mapping[GatewayTool, GatewayTarget]:
        targets = dict(self._runtime.gateway_targets())
        targets.update(
            {
                GatewayTool.LEAN_MULTI_ATTEMPT: self.multi_attempt,
                GatewayTool.DOCUMENT_APPLY: self.document_apply,
                GatewayTool.CONTRIBUTION_SUBMIT: self.contribution_submit,
                GatewayTool.KNOWLEDGE_READ: self.knowledge_read,
            }
        )
        return targets

    async def multi_attempt(self, call: AuthorizedCall) -> JsonValue:
        result = await runtime_multi_attempt(self._runtime, call)
        if call.lease_id is None:
            raise WorkerGatewayError("LEASE_REQUIRED")
        document_id = _text(call.payload, "document_id")
        accepted = self._accepted.setdefault(
            (call.run_id, call.worker_id, call.lease_id, document_id), set()
        )
        items = result.get("items") if type(result) is dict else None
        if type(items) is list:
            for item in items:
                snippet = _accepted_snippet(item)
                if snippet is not None:
                    accepted.add(snippet)
        return result

    async def document_apply(self, call: AuthorizedCall) -> JsonValue:
        if call.lease_id is None:
            raise WorkerGatewayError("LEASE_REQUIRED")
        document_id, accepted = _text(call.payload, "document_id"), _text(call.payload, "accepted")
        key = (call.run_id, call.worker_id, call.lease_id, document_id)
        import_module = _optional_text(call.payload, "import_module")
        if accepted not in self._accepted.get(key, set()) and import_module is None:
            raise WorkerGatewayError("UNVERIFIED_DOCUMENT_APPLY")
        if import_module is not None and not self._acknowledged_module(call, import_module):
            raise WorkerGatewayError("KNOWLEDGE_DELTA_REQUIRED")
        snapshot = await self._broker.read_document(
            call.run_id, call.worker_id, call.lease_id, document_id
        )
        replacement = _replace_sorry(snapshot.content, accepted, import_module)
        updated = await self._broker.compare_and_swap(
            call.run_id,
            call.worker_id,
            call.lease_id,
            document_id,
            snapshot.file_version,
            snapshot.content_hash,
            replacement,
        )
        return {
            "document_id": updated.document_id,
            "version": updated.file_version,
            "content_hash": updated.content_hash,
        }

    async def contribution_submit(self, call: AuthorizedCall) -> JsonValue:
        if call.lease_id is None:
            raise WorkerGatewayError("LEASE_REQUIRED")
        document_id = _text(call.payload, "document_id")
        snapshot = await self._broker.read_document(
            call.run_id, call.worker_id, call.lease_id, document_id
        )
        draft = ContributionDraft(
            _text(call.payload, "contribution_id"),
            call.worker_id,
            call.run_id,
            call.lease_id,
            document_id,
            snapshot.epoch_pair,
            self._environment,
            SnapshotPayload(snapshot.content, sha256_bytes(snapshot.content)),
            _text(call.payload, "candidate_name"),
            _text(call.payload, "complete_type"),
            _strings(call.payload, "imports"),
            _strings(call.payload, "dependencies"),
            _strings(call.payload, "assumptions"),
            _strings(call.payload, "evidence_links"),
        )
        service = ContributionService(
            self._state,
            self._artifacts,
            self._environment,
            tuple(
                dict.fromkeys(
                    ("Std", *(item.module for item in KnowledgeReader(self._state).read(0)))
                )
            ),
        )
        submitted = service.submit(draft)
        self._on_submission()
        return {
            "contribution_id": submitted.contribution_id,
            "enqueue_sequence": submitted.queue_entry.enqueue_sequence,
        }

    async def knowledge_read(self, call: AuthorizedCall) -> JsonValue:
        after = _integer(call.payload, "after_knowledge_epoch")
        wait = call.payload.get("wait", False)
        if type(wait) is not bool:
            raise WorkerGatewayError("INVALID_KNOWLEDGE_REQUEST")
        deltas = await self._knowledge.read(call.run_id, call.worker_id, after, wait=wait)
        return {"deltas": [_delta(item) for item in deltas]}

    def _acknowledged_module(self, call: AuthorizedCall, module: str) -> bool:
        acknowledged: set[str] = set()
        for record in self._state.query_events(call.run_id):
            event = record.envelope
            if event.event_type != "KnowledgeDeltaAcknowledged":
                continue
            if event.payload.get("worker_id") != call.worker_id:
                continue
            delta_id = event.payload.get("delta_id")
            if type(delta_id) is str:
                acknowledged.add(delta_id)
        return any(
            delta.delta_id in acknowledged and delta.module == module
            for delta in KnowledgeReader(self._state).read(0)
        )


def controller_worker_tools(role: AgentRole) -> tuple[GatewayTool, ...]:
    return tuple(
        tool
        for tool in advertised_tools(role)
        if tool in CONTROLLER_WORKER_TOOLS and tool in _WORKER_GATEWAY_TARGETS
    )


def _accepted_snippet(value: JsonValue) -> str | None:
    if type(value) is not dict:
        return None
    snippet = value.get("snippet")
    diagnostics = value.get("diagnostics")
    timed_out = value.get("timed_out")
    if type(snippet) is not str:
        return None
    if type(diagnostics) is not list or diagnostics or timed_out is not False:
        return None
    return snippet


def _replace_sorry(source: bytes, accepted: str, import_module: str | None) -> bytes:
    marker = b"  sorry"
    if source.count(marker) != 1:
        raise WorkerGatewayError("DOCUMENT_PROOF_MARKER_MISSING")
    result = source.replace(marker, f"  {accepted}".encode(), 1)
    if import_module is None:
        return result
    line = f"import {import_module}\n".encode()
    if line in result.splitlines(keepends=True):
        return result
    if not result.startswith(b"import Std\n"):
        raise WorkerGatewayError("DOCUMENT_IMPORT_LAYOUT_INVALID")
    return result.replace(b"import Std\n", b"import Std\n" + line, 1)


def _delta(item: PublishedKnowledgeDelta) -> dict[str, JsonValue]:
    return {
        "delta_id": item.delta_id,
        "previous_base_epoch": item.previous_epoch.base_epoch,
        "previous_knowledge_epoch": item.previous_epoch.knowledge_epoch,
        "base_epoch": item.new_epoch.base_epoch,
        "knowledge_epoch": item.new_epoch.knowledge_epoch,
        "fully_qualified_name": item.fully_qualified_name,
        "complete_type": item.complete_type,
        "module": item.module,
        "dependencies": list(item.dependencies),
        "assumptions": list(item.assumptions),
        "axioms": list(item.axioms),
        "evidence_links": list(item.evidence_links),
        "contribution_id": item.contribution_id,
        "publication_sequence": item.publication_sequence,
    }


def _text(payload: Mapping[str, JsonValue], field: str) -> str:
    value = payload.get(field)
    if type(value) is not str or not value:
        raise WorkerGatewayError("INVALID_GATEWAY_REQUEST")
    return value


def _optional_text(payload: Mapping[str, JsonValue], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    return _text(payload, field)


def _integer(payload: Mapping[str, JsonValue], field: str) -> int:
    value = payload.get(field)
    if type(value) is not int or value < 0:
        raise WorkerGatewayError("INVALID_GATEWAY_REQUEST")
    return value


def _strings(payload: Mapping[str, JsonValue], field: str) -> tuple[str, ...]:
    value = payload.get(field)
    if type(value) is not list:
        raise WorkerGatewayError("INVALID_GATEWAY_REQUEST")
    strings: list[str] = []
    for item in value:
        if type(item) is not str or not item:
            raise WorkerGatewayError("INVALID_GATEWAY_REQUEST")
        strings.append(item)
    return tuple(strings)
