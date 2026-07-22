from __future__ import annotations

import json
import re
from dataclasses import fields
from datetime import UTC, datetime

from aizim.agents import BackendIdentity
from aizim.config import AizimConfig
from aizim.domain import EpochPair, RunManifest
from aizim.modes.evaluation import EvaluationPolicy, ManifestInput
from aizim.modes.manifest import canonical_manifest, manifest_hash


def _input() -> ManifestInput:
    return ManifestInput(
        run_id="run-1",
        epoch_pair=EpochPair("a" * 64, 0),
        environment_fingerprint="b" * 64,
        started_at=datetime(2026, 7, 22, 12, tzinfo=UTC),
        config=AizimConfig(),
        backend=BackendIdentity("fake", "deterministic-v1", None),
        model_identifier="deterministic-fixture",
        goal="prove two smoke declarations",
        allowed_imports=("Std",),
        tool_surface=("document.apply", "contribution.submit", "lean.goal"),
        prompt_hashes=(("proof_worker", "c" * 64),),
        cache_state="cold",
        process_isolation_profile="deterministic-in-process",
    )


def test_manifest_is_canonical_and_stable_across_equivalent_reconstruction() -> None:
    policy = EvaluationPolicy(AizimConfig().run)

    first = policy.manifest(_input())
    second = EvaluationPolicy(AizimConfig().run).manifest(_input())

    assert canonical_manifest(first) == canonical_manifest(second)
    assert manifest_hash(first) == manifest_hash(second)


def test_manifest_contains_every_contract_field_and_separate_evaluation_labels() -> None:
    manifest = EvaluationPolicy(AizimConfig().run).manifest(_input())
    document = json.loads(canonical_manifest(manifest))

    assert set(document) == {field.name for field in fields(RunManifest)}
    assert document["mathlib_version"] is None
    assert re.fullmatch(r"[0-9a-f]{64}", document["agent_harness_binary_hash"])
    assert document["cache_state"] == "cold"
    assert document["process_isolation_profile"] == "deterministic-in-process"
    assert document["run_policy"]["formal_participation"] == "formal_unassisted"
    assert document["alignment_review"]["review_kind"] == "none"
    assert document["timeouts_seconds"] == [
        ["alignment_auditor", 60.0],
        ["proof_worker", 60.0],
    ]
    assert document["rejected_transition_requests"] == []


def test_manifest_records_hashed_rejected_transition_without_credential_material() -> None:
    policy = EvaluationPolicy(AizimConfig().run)
    rejection = policy.reject_transition("prompts", {"replacement": "different prompt"})
    manifest = policy.manifest(_input(), (rejection,))
    body = canonical_manifest(manifest).decode()

    assert rejection.manifest_entry in manifest.rejected_transition_requests
    assert "different prompt" not in body
    assert "capability_token" not in body
