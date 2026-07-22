from __future__ import annotations

from aizim import foundation_contract as contract
from aizim import foundation_evidence as fe
from aizim.domain import sha256_bytes, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.modes.formal_trace import read_formal_trace, replay_formal_trace

type Criterion = tuple[str, bool]


def _object(value: JsonValue) -> fe.JsonObject | None:
    return value if type(value) is dict else None


def _typed(events: tuple[fe.EventEvidence, ...], event_type: str) -> tuple[fe.EventEvidence, ...]:
    return tuple(event for event in events if event.event_type == event_type)


def _pairs(value: JsonValue) -> dict[str, JsonValue] | None:
    if type(value) is not list:
        return None
    parsed: dict[str, JsonValue] = {}
    for pair in value:
        if type(pair) is not list or len(pair) != 2 or type(pair[0]) is not str:
            return None
        parsed[pair[0]] = pair[1]
    return parsed if len(parsed) == len(value) else None


def _stable_manifest(start: fe.JsonObject, final: fe.JsonObject, run_id: str) -> bool:
    start_review = _object(start.get("alignment_review"))
    return (
        start_review is not None
        and start_review.get("review_kind") == "none"
        and start.get("run_id") == final.get("run_id") == run_id
        and all(field in start and field in final for field in contract.STABLE_MANIFEST_FIELDS)
        and all(start[field] == final[field] for field in contract.STABLE_MANIFEST_FIELDS)
    )


def _publications(events: tuple[fe.EventEvidence, ...]) -> bool:
    published = _typed(events, "DeclarationPublished")
    verified = _typed(events, "PromotionVerificationRecorded")
    if len(published) != 2 or len(verified) != 2:
        return False
    for declaration in published:
        contribution = declaration.payload.get("contribution_id")
        matches = tuple(
            item
            for item in verified
            if item.sequence < declaration.sequence
            and item.payload.get("contribution_id") == contribution
        )
        if type(contribution) is not str or len(matches) != 1:
            return False
        payload = matches[0].payload
        for kind in ("diagnostics", "build", "source_scan", "axiom_verification"):
            if payload.get(f"{kind}_verdict") != "pass" or not fe.is_hash(
                payload.get(f"{kind}_hash")
            ):
                return False
        if payload.get("source_scan_hash") != payload.get("axiom_verification_hash"):
            return False
    return True


def _epochs(events: tuple[fe.EventEvidence, ...], manifest: fe.JsonObject) -> bool:
    pair = _object(manifest.get("epoch_pair"))
    deltas = _typed(events, "KnowledgeDeltaPublished")
    if pair is None or len(deltas) != 2:
        return False
    base, knowledge = pair.get("base_epoch"), pair.get("knowledge_epoch")
    if not fe.is_hash(base) or knowledge != 0:
        return False
    for expected, delta in enumerate(deltas, 1):
        payload = delta.payload
        next_base = payload.get("base_epoch")
        if (
            payload.get("previous_base_epoch") != base
            or payload.get("previous_knowledge_epoch") != knowledge
            or payload.get("knowledge_epoch") != expected
            or payload.get("publication_sequence") != expected
            or not fe.is_hash(next_base)
        ):
            return False
        base, knowledge = next_base, expected
    return knowledge == 2


def _trace(events: tuple[fe.EventEvidence, ...], evidence: fe.FoundationEvidence) -> bool:
    try:
        body = fe.artifact(evidence, "formal-trace.jsonl").body
        expected = fe.artifact(evidence, "formal-trace.sha256").body.decode().strip()
        trace = read_formal_trace(body)
    except (UnicodeDecodeError, ValueError):
        return False
    by_id = {event.event_id: event for event in events}
    return (
        replay_formal_trace(trace, expected)
        and sum(item.kind == "source_scan" for item in trace) == 2
        and all(
            (source := by_id.get(item.source_event_id)) is not None
            and source.sequence == item.sequence
            and source.event_type == item.source_event_type
            and sha256_json(source.payload) == item.payload_hash
            for item in trace
        )
    )


def _completion_policy(events: tuple[fe.EventEvidence, ...], gate_hash: str) -> bool:
    completions = _typed(events, "AgentRunCompleted")
    workers = [item.payload.get("worker_id") for item in completions]
    return (
        len(completions) == 4
        and all(type(worker) is str for worker in workers)
        and tuple(sorted(workers)) == contract.REAL_COMPLETIONS
        and all(item.payload.get("policy_hash") == gate_hash for item in completions)
        and all(
            item.payload.get("status") == "submitted" and item.payload.get("exit_code") == 0
            for item in completions
        )
    )


def _workers(events: tuple[fe.EventEvidence, ...], manifest: fe.JsonObject) -> bool:
    resources = _object(manifest.get("resources"))
    starts = [item.payload.get("worker_id") for item in _typed(events, "WorkerStarted")]
    return (
        resources is not None
        and resources.get("lsp_instances") == 1
        and resources.get("max_proof_workers") == 2
        and len(_typed(events, "LeanRuntimeStarted")) == 1
        and all(type(worker) is str for worker in starts)
        and tuple(sorted(starts)) == contract.PROOF_EXECUTIONS
    )


def _pins(manifest: fe.JsonObject) -> bool:
    dependencies = _pairs(manifest.get("dependency_versions"))
    settings = _pairs(manifest.get("reasoning_settings"))
    resources = _object(manifest.get("resources"))
    return (
        dependencies == {"lean-lsp-mcp": "0.28.1", "leanclient": "0.12.1", "mcp": "1.28.1"}
        and settings is not None
        and settings.get("allowed_imports_hash") == sha256_json(("Std",))
        and manifest.get("lean_version") == "4.32.0"
        and manifest.get("toolchain_version") == "leanprover/lean4:v4.32.0"
        and manifest.get("mathlib_version") is None
        and manifest.get("repl_revision") is None
        and resources is not None
        and resources.get("local_loogle") is False
    )


def _acknowledgement(events: tuple[fe.EventEvidence, ...]) -> bool:
    first = [
        item
        for item in _typed(events, "KnowledgeDeltaPublished")
        if item.payload.get("publication_sequence") == 1
    ]
    second = [
        item
        for item in _typed(events, "DeclarationPublished")
        if item.payload.get("publication_sequence") == 2
    ]
    if len(first) != 1 or len(second) != 1:
        return False
    return any(
        first[0].sequence < item.sequence < second[0].sequence
        and item.payload.get("worker_id") == "prover-b"
        and item.payload.get("delta_id") == first[0].payload.get("delta_id")
        for item in _typed(events, "KnowledgeDeltaAcknowledged")
    )


def _alignment(
    events: tuple[fe.EventEvidence, ...], manifest: fe.JsonObject, body: fe.JsonObject
) -> bool:
    reviews = _typed(events, "AlignmentReviewed")
    final = _object(manifest.get("alignment_review"))
    if len(reviews) != 1 or final is None:
        return False
    review = reviews[0].payload
    reviewer = "codex-alignment-auditor"
    return (
        review.get("kind") == body.get("review_kind") == final.get("review_kind") == "machine"
        and review.get("verdict") == body.get("verdict") == final.get("verdict") == "aligned"
        and review.get("reviewer") == body.get("reviewer") == reviewer
        and final.get("reviewer_identity") == reviewer
        and final.get("protocol_revision") == "alignment-v1"
    )


def _codex(manifest: fe.JsonObject) -> bool:
    policy = _object(manifest.get("run_policy"))
    return (
        manifest.get("agent_harness_name") == manifest.get("model_backend") == "codex"
        and manifest.get("agent_harness_version") == "codex-cli 0.144.6"
        and fe.is_hash(manifest.get("agent_harness_binary_hash"))
        and type(manifest.get("model_identifier")) is str
        and manifest.get("process_isolation_profile") == "macos-sandbox-aizim-worker"
        and policy is not None
        and policy.get("participation") == "autonomous"
        and policy.get("formal_participation") == "formal_unassisted"
        and policy.get("lean_runtime") == "shared"
        and policy.get("human_interventions") == 0
        and policy.get("environment_frozen") is True
    )


def _shutdown(events: tuple[fe.EventEvidence, ...]) -> bool:
    stopped = [item.payload.get("worker_id") for item in _typed(events, "WorkerStopped")]
    return (
        all(type(worker) is str for worker in stopped)
        and tuple(sorted(stopped)) == contract.PROOF_EXECUTIONS
        and len(_typed(events, "LeaseReleased")) == 3
        and len(_typed(events, "LeanRuntimeStopped")) == 1
    )


def _report(evidence: fe.FoundationEvidence, report: fe.JsonObject) -> bool:
    smoke = report.get("engineering_smoke_statement")
    manifest = fe.artifact(evidence, "run-manifest.json")
    trace = fe.artifact(evidence, "formal-trace.jsonl")
    return (
        report.get("kernel_verdict") == "pass"
        and report.get("alignment_verdict") == "aligned"
        and report.get("evaluation_verdict") == "pass"
        and report.get("participation_label") == "formal_unassisted"
        and report.get("manifest_hash") == manifest.digest
        and report.get("trace_hash") == sha256_bytes(trace.body)
        and type(smoke) is str
        and "engineering smoke test" in smoke
        and "not evidence" in smoke
        and "open-problem" in smoke
        and "novelty" in smoke
    )


def evaluate(evidence: fe.FoundationEvidence) -> tuple[Criterion, ...]:
    selection = evidence.selection
    events = tuple(event for event in evidence.events if event.run_id == selection.run_id)
    manifest = fe.json_artifact(evidence, "run-manifest.json")
    alignment = fe.json_artifact(evidence, "alignment-review.json")
    report = fe.json_artifact(evidence, "acceptance-report.json")
    gate_hash = fe.gate_policy(evidence.events, selection.created_sequence)
    publications = _typed(events, "DeclarationPublished")
    completed = _typed(events, "RunCompleted")
    stable = _stable_manifest(selection.manifest, manifest, selection.run_id)
    return (
        ("01-package-and-registered-artifacts", len(evidence.artifacts) == 5),
        ("02-state-service-rpc-replay", fe.is_hash(evidence.replay_digest)),
        ("03-genuine-real-run-selection", stable),
        ("04-positive-gate-b-policy", fe.is_hash(gate_hash)),
        ("05-identical-real-backend-policy", _completion_policy(events, gate_hash)),
        ("06-one-lsp-two-proof-workers", _workers(events, manifest)),
        ("07-pinned-lean-and-dependencies", _pins(manifest)),
        ("08-publication-verification-before-write", _publications(events)),
        ("09-total-epoch-delta-sequence", _epochs(events, manifest)),
        ("10-replayable-formal-trace", _trace(events, evidence)),
        (
            "11-deterministic-publication-order",
            [item.payload.get("publication_sequence") for item in publications] == [1, 2],
        ),
        ("12-worker-b-delta-consumption", _acknowledgement(events)),
        (
            "13-failure-free-terminal-run",
            not any(event.event_type in contract.FAILURE_EVENT_TYPES for event in events)
            and len(completed) == 1
            and completed[0].payload.get("outcome") == "PASS"
            and _shutdown(events)
            and all(item.sequence < completed[0].sequence for item in publications),
        ),
        ("14-machine-alignment-review", _alignment(events, manifest, alignment)),
        ("15-real-codex-unassisted-manifest", _codex(manifest)),
        ("16-engineering-smoke-limitation", _report(evidence, report)),
    )
