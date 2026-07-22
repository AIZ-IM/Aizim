from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aizim.modes.formal_trace import read_formal_trace, replay_formal_trace
from aizim.modes.manifest import SMOKE_TEST_LIMITATION
from aizim.state import StateService, StateServiceConfig

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"
_ARTIFACTS = {
    "run-manifest.json",
    "formal-trace.jsonl",
    "formal-trace.sha256",
    "alignment-review.json",
    "acceptance-report.json",
}


def _cli(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "aizim", *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
        env=dict(os.environ),
    )


def _copy_smoke(tmp_path: Path) -> Path:
    return Path(
        shutil.copytree(SMOKE_ROOT, tmp_path / "smoke", ignore=shutil.ignore_patterns(".aizim"))
    )


@pytest.mark.manual_real_codex
@pytest.mark.lean_integration
@pytest.mark.macos_sandbox
def test_real_codex_shared_smoke_is_kernel_verified_and_replayable(tmp_path: Path) -> None:
    if not os.environ.get("AIZIM_MODEL"):
        pytest.skip("AIZIM_MODEL is required for the manual real-Codex gate")
    project = _copy_smoke(tmp_path)

    run = _cli(
        "run",
        "--project",
        str(project),
        "--profile",
        "autonomous-shared",
        "--backend",
        "codex",
        cwd=project,
    )
    status = _cli("status", "--project", str(project), "--json", cwd=project)

    assert run.returncode == 0, run.stderr
    assert "AIZIM RUN PASS" in run.stdout
    assert SMOKE_TEST_LIMITATION in run.stdout
    assert status.returncode == 0, status.stderr
    document = json.loads(status.stdout)
    assert document["epochs"]["knowledge_epoch"] == 2
    assert len(document["verified_declarations"]) == 2
    with StateService(StateServiceConfig(project, "real-codex-read")) as state:
        run_id = next(
            record.envelope.run_id
            for record in state.query_events()
            if record.envelope.event_type == "RunCreated"
        )
        assert run_id is not None
        events = state.query_events(run_id)

    root = project / ".aizim" / "artifacts" / run_id
    manifest = json.loads((root / "run-manifest.json").read_text())
    trace = read_formal_trace((root / "formal-trace.jsonl").read_bytes())
    event_types = [record.envelope.event_type for record in events]
    alignment = next(
        record.envelope.payload
        for record in events
        if record.envelope.event_type == "AlignmentReviewed"
    )

    assert {item.name for item in root.iterdir()} >= _ARTIFACTS
    assert manifest["agent_harness_name"] == "codex"
    assert len(manifest["agent_harness_binary_hash"]) == 64
    assert manifest["model_backend"] == "codex"
    assert manifest["run_policy"]["formal_participation"] == "formal_unassisted"
    assert manifest["run_policy"]["lean_runtime"] == "shared"
    assert manifest["run_policy"]["human_interventions"] == 0
    assert manifest["mathlib_version"] is None
    assert manifest["process_isolation_profile"] == "macos-sandbox-aizim-worker"
    assert manifest["alignment_review"]["review_kind"] == "machine"
    assert manifest["alignment_review"]["verdict"] == "aligned"
    assert alignment["kind"] == "machine"
    assert alignment["reviewer"] == "codex-alignment-auditor"
    assert alignment["verdict"] == "aligned"
    assert event_types.count("DeclarationPublished") == 2
    assert event_types.count("PromotionVerificationRecorded") == 2
    completions = sorted(
        worker_id
        for record in events
        if record.envelope.event_type == "AgentRunCompleted"
        and type(worker_id := record.envelope.payload["worker_id"]) is str
    )
    starts = sorted(
        worker_id
        for record in events
        if record.envelope.event_type == "WorkerStarted"
        and type(worker_id := record.envelope.payload["worker_id"]) is str
    )
    assert completions == ["alignment-auditor", "prover-a", "prover-b", "prover-b"]
    assert starts == ["prover-a", "prover-b", "prover-b"]
    assert event_types.count("WorkerStopped") == 3
    assert event_types.count("LeaseReleased") == 3
    assert event_types.count("LeanRuntimeStopped") == 1
    assert event_types.count("RunCompleted") == 1
    assert "RunAborted" not in event_types
    first_delta = next(
        record.envelope.payload["delta_id"]
        for record in events
        if record.envelope.event_type == "KnowledgeDeltaPublished"
        and record.envelope.payload["knowledge_epoch"] == 1
    )
    assert any(
        record.envelope.event_type == "KnowledgeDeltaAcknowledged"
        and record.envelope.payload["worker_id"] == "prover-b"
        and record.envelope.payload["delta_id"] == first_delta
        for record in events
    )
    for record in events:
        if record.envelope.event_type != "PromotionVerificationRecorded":
            continue
        payload = record.envelope.payload
        assert payload["diagnostics_verdict"] == "pass"
        assert payload["build_verdict"] == "pass"
        assert payload["axiom_verification_verdict"] == "pass"
        for field in ("diagnostics_hash", "build_hash", "axiom_verification_hash"):
            value = payload[field]
            assert type(value) is str and len(value) == 64
    assert "InterventionRecorded" not in event_types
    assert "CapabilityDenied" not in event_types
    assert "WorkerCrashed" not in event_types
    assert "SandboxProbeFailed" not in event_types
    assert "EnvironmentTransitionApproved" not in event_types
    assert "EnvironmentTransitionRejected" not in event_types
    assert replay_formal_trace(trace, (root / "formal-trace.sha256").read_text().strip())
    report = json.loads((root / "acceptance-report.json").read_text())
    assert report["kernel_verdict"] == "pass"
    assert report["alignment_verdict"] == "aligned"
    assert report["participation_label"] == "formal_unassisted"
    assert "engineering smoke test" in report["engineering_smoke_statement"]
