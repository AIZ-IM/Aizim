from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from aizim.agents import BackendIdentity
from aizim.config import AizimConfig
from aizim.domain import EpochPair, FileLease, sha256_bytes, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.lean.project import smoke_base_epoch
from aizim.modes.evaluation import EvaluationPolicy, ManifestInput
from aizim.modes.formal_trace import (
    build_formal_trace,
    formal_trace_bytes,
    read_formal_trace,
    replay_formal_trace,
)
from aizim.modes.manifest import (
    acceptance_report_bytes,
    alignment_review_bytes,
    canonical_manifest,
    manifest_document,
    manifest_hash,
    register_artifact,
    write_named_artifact,
)
from aizim.orchestration.evaluation_contract import (
    PROOF_RUN_TOOLS,
    environment_fingerprint,
)
from aizim.state import AppendEventCommand, StateService, StateServiceConfig

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"
_ARTIFACTS = {
    "run-manifest.json",
    "formal-trace.jsonl",
    "formal-trace.sha256",
    "alignment-review.json",
    "acceptance-report.json",
}


def _copy_smoke(tmp_path: Path) -> Path:
    return Path(
        shutil.copytree(SMOKE_ROOT, tmp_path / "smoke", ignore=shutil.ignore_patterns(".aizim"))
    )


def _record_historical_run(state: StateService, manifest_input: ManifestInput) -> None:
    """Reconstruct schema-v1 history without retaining its retired executor."""
    run_id, epoch = manifest_input.run_id, manifest_input.epoch_pair
    policy = EvaluationPolicy(manifest_input.config.run)
    state.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {"project_id": "project", "base_epoch": epoch.base_epoch, "knowledge_epoch": 0},
        )
    )
    state.append_event(
        AppendEventCommand(
            "RunCreated",
            "research_conductor",
            run_id,
            None,
            {"manifest": manifest_document(policy.manifest(manifest_input))},
        )
    )
    source = b"import Std\ntheorem candidate : True := True.intro\n"
    source_hash = sha256_bytes(source)
    state.grant_document_lease(
        FileLease(
            "lease-1",
            "worker-1",
            run_id,
            "document-1",
            "AizimSmoke.Workers.W_worker",
            epoch,
            0,
            source_hash,
            datetime(2030, 1, 1, tzinfo=UTC),
        ),
        PurePosixPath(f"AizimSmoke/Workers/{run_id}/worker-1.lean"),
    )
    events: list[tuple[str, dict[str, JsonValue]]] = [
        (
            "ScheduleProposed",
            {
                "directive_id": "directive-1",
                "worker_id": "worker-1",
                "execution_id": "execution-1",
                "role": "proof_explorer",
            },
        ),
        ("WorkerStarted", {"worker_id": "worker-1", "role": "proof_explorer"}),
    ]
    for action_kind in ("goal", "trial"):
        events.append(
            (
                "FormalActionRecorded",
                {
                    "action_id": f"action-{action_kind}",
                    "action_kind": action_kind,
                    "worker_id": "worker-1",
                    "document_id": "document-1",
                    "document_version": 0,
                    "base_epoch": epoch.base_epoch,
                    "knowledge_epoch": 0,
                    "input_hash": source_hash,
                    "output_hash": sha256_json({"action": action_kind, "result": "True"}),
                    "verdict": "success",
                    "started_at": "2026-07-22T00:00:00Z",
                    "completed_at": "2026-07-22T00:00:01Z",
                },
            )
        )
    completion_events: tuple[tuple[str, dict[str, JsonValue]], ...] = (
        (
            "ContributionSubmitted",
            {
                "contribution_id": "contribution-1",
                "worker_id": "worker-1",
                "candidate_name": "candidate",
                "complete_type": "True",
                "payload_hash": source_hash,
            },
        ),
        (
            "PromotionVerificationRecorded",
            {
                "contribution_id": "contribution-1",
                "diagnostics_hash": sha256_json({"diagnostics": []}),
                "diagnostics_verdict": "pass",
                "build_hash": sha256_json({"build": "pass"}),
                "build_verdict": "pass",
                "source_scan_hash": source_hash,
                "source_scan_verdict": "pass",
                "axiom_verification_hash": sha256_json({"axioms": []}),
                "axiom_verification_verdict": "pass",
            },
        ),
        (
            "DeclarationPublished",
            {
                "declaration_id": "declaration-1",
                "contribution_id": "contribution-1",
                "content_hash": source_hash,
                "publication_sequence": 1,
            },
        ),
        (
            "KnowledgeDeltaPublished",
            {
                "delta_id": "delta-1",
                "declaration_id": "declaration-1",
                "contribution_id": "contribution-1",
                "base_epoch": sha256_bytes(source),
                "knowledge_epoch": 1,
                "previous_base_epoch": epoch.base_epoch,
                "previous_knowledge_epoch": 0,
                "publication_sequence": 1,
            },
        ),
        (
            "AlignmentReviewed",
            {
                "review_id": "review-1",
                "kind": "machine",
                "verdict": "aligned",
                "reviewer": "historical-auditor",
            },
        ),
        ("RunCompleted", {"outcome": "PASS"}),
    )
    events.extend(completion_events)
    for event_type, payload in events:
        state.append_event(
            AppendEventCommand(event_type, "historical_replay", run_id, None, payload)
        )


def test_historical_run_writes_registered_replayable_formal_trace(tmp_path: Path) -> None:
    project = _copy_smoke(tmp_path)
    run_id = "historical-formal-trace"
    config = AizimConfig()
    manifest_input = ManifestInput(
        run_id,
        EpochPair(smoke_base_epoch(project), 0),
        environment_fingerprint(project),
        datetime(2026, 7, 22, tzinfo=UTC),
        config,
        BackendIdentity("codex", "codex-cli 0.154.0", "c" * 64),
        "historical-model",
        "Replay the recorded proof of True",
        ("Std",),
        tuple(tool.value for tool in PROOF_RUN_TOOLS),
        (("proof_worker", sha256_json("historical proof prompt")),),
        "cold",
        "aizim-worker",
    )
    policy = EvaluationPolicy(config.run)
    with StateService(StateServiceConfig(project, "formal-trace-write")) as state:
        _record_historical_run(state, manifest_input)
        records = state.query_events(run_id)
        labels = policy.labels(records)
        evaluated = policy.evaluated_manifest(manifest_input, records)
        trace_body = formal_trace_bytes(build_formal_trace(records))
        trace_digest = sha256_bytes(trace_body)
        verified = sum(record.envelope.event_type == "DeclarationPublished" for record in records)
        bodies = {
            "run-manifest.json": canonical_manifest(evaluated),
            "formal-trace.jsonl": trace_body,
            "formal-trace.sha256": (trace_digest + "\n").encode(),
            "alignment-review.json": alignment_review_bytes(labels),
            "acceptance-report.json": acceptance_report_bytes(
                labels, verified, manifest_hash(evaluated), trace_digest
            ),
        }
        for name, body in bodies.items():
            media_type = (
                "application/x-ndjson" if name.endswith(".jsonl")
                else "text/plain" if name.endswith(".sha256")
                else "application/json"
            )
            register_artifact(
                state, run_id, write_named_artifact(project, run_id, name, body, media_type)
            )

    with StateService(StateServiceConfig(project, "formal-trace-read")) as state:
        records = state.query_events(run_id)

    root = project / ".aizim" / "artifacts" / run_id
    trace = read_formal_trace((root / "formal-trace.jsonl").read_bytes())
    manifest = json.loads((root / "run-manifest.json").read_text())
    registered = {
        name
        for record in records
        if record.envelope.event_type == "ArtifactRegistered"
        and type(name := record.envelope.payload["artifact_name"]) is str
    }

    assert {path.name for path in root.iterdir()} >= _ARTIFACTS
    assert registered == _ARTIFACTS
    assert manifest["environment_fingerprint"] == environment_fingerprint(project)
    assert manifest["environment_fingerprint"] != "e" * 64
    settings = dict(manifest["reasoning_settings"])
    assert settings["tool_surface_hash"] == sha256_json(
        tuple(tool.value for tool in PROOF_RUN_TOOLS)
    )
    assert manifest["agent_harness_name"] == "codex"
    assert manifest["alignment_review"]["review_kind"] == "machine"
    assert manifest["alignment_review"]["verdict"] == "aligned"
    event_types = [record.envelope.event_type for record in records]
    assert event_types.count("RunCompleted") == 1
    assert "RunAborted" not in event_types
    assert replay_formal_trace(trace, (root / "formal-trace.sha256").read_text().strip())
    assert trace == build_formal_trace(
        tuple(record for record in records if record.envelope.event_type != "ArtifactRegistered")
    )
    assert {record.kind for record in trace} >= {
        "run",
        "directive",
        "worker",
        "lease",
        "goal",
        "trial",
        "contribution",
        "diagnostics",
        "build",
        "source_scan",
        "axiom_verification",
        "publication",
        "epoch",
        "delta",
        "alignment_review",
        "terminal",
    }
    assert all(record.source_event_id and record.content_hashes for record in trace)
    verification = {
        record.kind: record.source_event_type
        for record in trace
        if record.kind in {"diagnostics", "build", "source_scan", "axiom_verification"}
    }
    assert verification == {
        "diagnostics": "PromotionVerificationRecorded",
        "build": "PromotionVerificationRecorded",
        "source_scan": "PromotionVerificationRecorded",
        "axiom_verification": "PromotionVerificationRecorded",
    }
    acceptance = json.loads((root / "acceptance-report.json").read_text())
    assert acceptance["manifest_hash"] == manifest_hash(evaluated)
    assert acceptance["trace_hash"] == trace_digest
    assert acceptance["evaluation_verdict"] == "pass"
    assert acceptance["kernel_verdict"] == "failed"
