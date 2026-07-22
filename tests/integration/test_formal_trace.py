from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from aizim.domain import sha256_json
from aizim.modes.formal_trace import read_formal_trace, replay_formal_trace
from aizim.orchestration.evaluation_contract import (
    PROOF_RUN_TOOLS,
    environment_fingerprint,
)
from aizim.orchestration.runner import run_autonomous_shared
from aizim.state import StateService, StateServiceConfig

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


@pytest.mark.lean_integration
def test_fake_run_writes_registered_replayable_formal_trace(tmp_path: Path) -> None:
    project = _copy_smoke(tmp_path)

    assert run_autonomous_shared(project, "fake") == 0
    with StateService(StateServiceConfig(project, "formal-trace-read")) as state:
        run_id = next(
            record.envelope.run_id
            for record in state.query_events()
            if record.envelope.event_type == "RunCreated"
        )
        assert run_id is not None
        records = state.query_events(run_id)

    root = project / ".aizim" / "artifacts" / run_id
    trace_path = root / "formal-trace.jsonl"
    trace = read_formal_trace(trace_path.read_bytes())
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
    assert manifest["alignment_review"]["review_kind"] == "machine"
    assert manifest["alignment_review"]["verdict"] == "aligned"
    event_types = [record.envelope.event_type for record in records]
    assert event_types.count("RunCompleted") == 1
    assert "RunAborted" not in event_types
    assert replay_formal_trace(trace, (root / "formal-trace.sha256").read_text().strip())
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
