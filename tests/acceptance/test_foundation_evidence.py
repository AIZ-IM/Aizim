from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

import pytest

from aizim.domain import canonical_json, sha256_bytes, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.modes.formal_trace import FormalTraceRecord, build_formal_trace, formal_trace_bytes
from aizim.modes.manifest import RUNTIME_ACCEPTANCE_SCOPE, NamedArtifact, register_artifact
from aizim.state import AppendEventCommand, StateService, StateServiceConfig

REPOSITORY_ROOT = Path(__file__).parents[2]
CHECKER = REPOSITORY_ROOT / "scripts" / "check_foundation.py"
HASHES = tuple(f"{index:x}" * 64 for index in range(1, 10))
FOUNDATION_SOURCES = (
    CHECKER,
    REPOSITORY_ROOT / "src" / "aizim" / "foundation_checks.py",
    REPOSITORY_ROOT / "src" / "aizim" / "foundation_contract.py",
    REPOSITORY_ROOT / "src" / "aizim" / "foundation_epoch.py",
    REPOSITORY_ROOT / "src" / "aizim" / "foundation_evidence.py",
    REPOSITORY_ROOT / "src" / "aizim" / "foundation_trace.py",
)
FOUNDATION_RUNBOOK = REPOSITORY_ROOT / "docs" / "operations" / "foundation-runbook.md"
RECOVERY_RUNBOOK = REPOSITORY_ROOT / "docs" / "operations" / "event-recovery.md"


@dataclass(frozen=True, slots=True)
class _GateFixture:
    run_id: str
    policy_hash: str
    defect: str | None = None


@pytest.fixture
def foundation_project() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="aizim-foundation-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.mark.parametrize("source", FOUNDATION_SOURCES)
def test_foundation_production_sources_stay_within_pure_loc_limit(source: Path) -> None:
    pure_lines = sum(
        bool(line.strip()) and not line.lstrip().startswith("#")
        for line in source.read_text().splitlines()
    )

    assert pure_lines <= 250


def _append_run_evidence(
    state: StateService,
    run_id: str,
    defect: str | None = None,
    start_epoch: int = 0,
) -> bytes:
    start_manifest: dict[str, JsonValue] = {
        "agent_harness_binary_hash": HASHES[0],
        "agent_harness_name": "codex",
        "agent_harness_version": "codex-cli 0.144.6",
        "alignment_review": {
            "protocol_revision": None,
            "review_kind": "none",
            "reviewer_identity": None,
            "verdict": None,
        },
        "budgets": [["proof_worker_actions", 12], ["proof_workers", 2]],
        "cache_state": "cold",
        "capability_profile": "gateway-fixture",
        "dependency_versions": [
            ["mcp", "1.28.1"],
            ["lean-lsp-mcp", "0.28.1"],
            ["leanclient", "0.12.1"],
        ],
        "environment_fingerprint": HASHES[8],
        "environment_transition_policy": "reject",
        "epoch_pair": {"base_epoch": HASHES[1], "knowledge_epoch": start_epoch},
        "event_schema_version": 1,
        "lean_lsp_mcp_version": "0.28.1",
        "lean_version": "4.32.0",
        "leanclient_version": "0.12.1",
        "mathlib_version": None,
        "model_backend": "codex",
        "model_identifier": "fixture-model",
        "process_isolation_profile": "macos-sandbox-aizim-worker",
        "prompts": [["proof_worker", HASHES[2]], ["alignment_auditor", HASHES[3]]],
        "reasoning_settings": [
            ["allowed_imports_hash", sha256_json(("Std",))],
            ["goal_hash", HASHES[4]],
            ["tool_surface_hash", HASHES[5]],
        ],
        "rejected_transition_requests": [],
        "repl_revision": None,
        "resources": {
            **({} if defect == "missing-local-loogle" else {"local_loogle": False}),
            "lsp_instances": 1,
            "max_proof_workers": 2,
            "max_question_workers": 3,
            "min_free_disk_bytes": 2_147_483_648,
            "remote_search_max_concurrency": 1,
            "scratch_slots": 2,
        },
        "run_id": run_id,
        "run_policy": {
            "environment_frozen": True,
            "formal_participation": "formal_unassisted",
            "human_interventions": 0,
            "lean_runtime": "shared",
            "participation": "autonomous",
        },
        "search_permissions": [],
        "started_at": "2026-07-22T00:00:00+00:00",
        "timeouts_seconds": [["alignment_auditor", 60.0], ["proof_worker", 60.0]],
        "toolchain_version": "leanprover/lean4:v4.32.0",
    }
    state.append_event(
        AppendEventCommand(
            "RunCreated", "research_conductor", run_id, None, {"manifest": start_manifest}
        )
    )
    state.append_event(
        AppendEventCommand(
            "LeanRuntimeStarted", "lean_runtime", run_id, None, {"runtime_id": "lsp-1"}
        )
    )
    for execution, worker_id in enumerate(("prover-a", "prover-b"), start=1):
        state.append_event(
            AppendEventCommand(
                "ScheduleProposed",
                "research_conductor",
                run_id,
                None,
                {
                    "directive_id": f"directive-{execution}",
                    "execution_id": f"execution-{execution}",
                    "role": "proof_explorer",
                    "worker_id": worker_id,
                },
            )
        )
        state.append_event(
            AppendEventCommand(
                "WorkerStarted",
                "research_conductor",
                run_id,
                None,
                {"role": "proof_explorer", "worker_id": worker_id},
            )
        )
    previous_base = HASHES[1]
    for index, (worker_id, contribution_id, declaration_id, delta_id) in enumerate(
        (
            ("prover-a", "contribution-a", "declaration-a", "delta-a"),
            ("prover-b", "contribution-b", "declaration-b", "delta-b"),
        ),
        start=1,
    ):
        for action_kind in (("goal", "trial") if index == 1 else ("goal",)):
            state.append_event(
                AppendEventCommand(
                    "FormalActionRecorded",
                    worker_id,
                    run_id,
                    None,
                    {
                        "action_id": f"action-{index}-{action_kind}",
                        "action_kind": action_kind,
                        "base_epoch": previous_base,
                        "completed_at": "2026-07-22T00:00:01Z",
                        "document_id": f"document-{index}",
                        "document_version": 0,
                        "input_hash": HASHES[2],
                        "knowledge_epoch": start_epoch + index - 1,
                        "output_hash": HASHES[3],
                        "started_at": "2026-07-22T00:00:00Z",
                        "verdict": "success",
                        "worker_id": worker_id,
                    },
                )
            )
        state.append_event(
            AppendEventCommand(
                "DocumentEdited",
                worker_id,
                run_id,
                None,
                {
                    "base_epoch": previous_base,
                    "content_hash": HASHES[4],
                    "document_id": f"document-{index}",
                    "expires_at": "2026-07-22T00:05:00Z",
                    "knowledge_epoch": start_epoch + index - 1,
                    "lease_id": f"lease-{index}",
                    "relative_path": f"AizimSmoke/Workers/{run_id}/{worker_id}.lean",
                    "version": 1,
                    "virtual_document_namespace": f"AizimSmoke.Workers.W_{index}",
                    "worker_id": worker_id,
                },
            )
        )
        state.append_event(
            AppendEventCommand(
                "ContributionSubmitted",
                worker_id,
                run_id,
                None,
                {"contribution_id": contribution_id},
            )
        )
        verification_payload: dict[str, JsonValue] = {
            "axiom_verification_hash": HASHES[4],
            "axiom_verification_verdict": "pass",
            "build_hash": HASHES[3],
            "build_verdict": "pass",
            "contribution_id": contribution_id,
            "diagnostics_hash": HASHES[2],
            "diagnostics_verdict": "pass",
            "source_scan_hash": HASHES[4],
            "source_scan_verdict": "pass",
        }
        if index == 2 and defect == "missing-source-scan":
            del verification_payload["source_scan_hash"]
            del verification_payload["source_scan_verdict"]
        if index == 2 and defect == "failed-source-scan":
            verification_payload["source_scan_verdict"] = "failed"
        state.append_event(
            AppendEventCommand(
                "PromotionVerificationRecorded",
                "promotion_service",
                run_id,
                None,
                verification_payload,
            )
        )
        if defect == "extra-verification" and index == 1:
            state.append_event(
                AppendEventCommand(
                    "PromotionVerificationRecorded",
                    "promotion_service",
                    run_id,
                    None,
                    verification_payload,
                )
            )
        state.append_event(
            AppendEventCommand(
                "DeclarationPublished",
                "promotion_service",
                run_id,
                None,
                {
                    "contribution_id": contribution_id,
                    "declaration_id": declaration_id,
                    "publication_sequence": start_epoch + index,
                },
            )
        )
        next_base = HASHES[5 + index]
        state.append_event(
            AppendEventCommand(
                "KnowledgeDeltaPublished",
                "promotion_service",
                run_id,
                None,
                {
                    "base_epoch": next_base,
                    "contribution_id": contribution_id,
                    "declaration_id": declaration_id,
                    "delta_id": delta_id,
                    "knowledge_epoch": start_epoch + index,
                    "previous_base_epoch": previous_base,
                    "previous_knowledge_epoch": start_epoch + index - 1,
                    "publication_sequence": start_epoch + index,
                },
            )
        )
        previous_base = next_base
        if index == 1:
            state.append_event(
                AppendEventCommand(
                    "ScheduleProposed",
                    "research_conductor",
                    run_id,
                    None,
                    {
                        "directive_id": "directive-3",
                        "execution_id": "execution-3",
                        "role": "proof_explorer",
                        "worker_id": "prover-b",
                    },
                )
            )
            state.append_event(
                AppendEventCommand(
                    "KnowledgeDeltaAcknowledged",
                    "prover-b",
                    run_id,
                    None,
                    {
                        "acknowledgement_id": HASHES[8],
                        "delta_id": "delta-a",
                        "worker_id": "prover-b",
                    },
                )
            )
            state.append_event(
                AppendEventCommand(
                    "WorkerStarted",
                    "research_conductor",
                    run_id,
                    None,
                    {"role": "proof_explorer", "worker_id": "prover-b"},
                )
            )
    for execution, worker_id in enumerate(
        ("prover-a", "prover-b", "prover-b", "alignment-auditor"), start=1
    ):
        state.append_event(
            AppendEventCommand(
                "AgentRunCompleted",
                "worker_runner",
                run_id,
                None,
                {
                    "execution_id": f"execution-{execution}-{worker_id}",
                    "exit_code": 0,
                    "final_message_hash": HASHES[3],
                    "policy_hash": (
                        HASHES[6]
                        if defect == "latest-valid-gate"
                        or (
                            defect == "mismatched-completion-policy"
                            and worker_id == "alignment-auditor"
                        )
                        else HASHES[7]
                    ),
                    "status": "submitted",
                    "transport_event_hash": HASHES[2],
                    "worker_id": worker_id,
                },
            )
        )
    state.append_event(
        AppendEventCommand(
            "AlignmentReviewed",
            "alignment_auditor",
            run_id,
            None,
            {
                "kind": "machine",
                "review_id": "alignment-1",
                "reviewer": "codex-alignment-auditor",
                "verdict": "aligned",
            },
        )
    )
    if defect != "missing-shutdown":
        for worker_id in ("prover-a", "prover-b", "prover-b"):
            state.append_event(
                AppendEventCommand(
                    "WorkerStopped", "worker_runner", run_id, None, {"worker_id": worker_id}
                )
            )
        for lease_id in ("lease-a", "lease-b-wait", "lease-b-prove"):
            state.append_event(
                AppendEventCommand(
                    "LeaseReleased", "document_broker", run_id, None, {"lease_id": lease_id}
                )
            )
        state.append_event(
            AppendEventCommand(
                "LeanRuntimeStopped", "lean_runtime", run_id, None, {"runtime_id": "lsp-1"}
            )
        )
    state.append_event(
        AppendEventCommand(
            "RunCompleted",
            "evaluation_policy",
            run_id,
            None,
            {"outcome": "FAIL" if defect == "failed-run-outcome" else "PASS"},
        )
    )
    manifest = {
        **start_manifest,
        "alignment_review": {
            "protocol_revision": "alignment-v1",
            "review_kind": "machine",
            "reviewer_identity": "codex-alignment-auditor",
            "verdict": "aligned",
        },
    }
    manifest_body = canonical_json(manifest)
    return manifest_body


def _append_gate_terminal(state: StateService, fixture: _GateFixture) -> None:
    terminal_type = (
        "SandboxProbeFailed"
        if fixture.defect == "failed-gate-after-attempts"
        else "SandboxProbePassed"
    )
    terminal_payload: dict[str, JsonValue] = {"probe_id": "authority-probe:gate_b_complete"}
    if terminal_type == "SandboxProbeFailed":
        terminal_payload["reason_code"] = "AGGREGATE_EVIDENCE_FAILED"
    else:
        terminal_payload.update(
            {"operation": "gate_b_complete", "policy_hash": fixture.policy_hash}
        )
    state.append_event(
        AppendEventCommand(
            terminal_type,
            "security_gate",
            fixture.run_id,
            None,
            terminal_payload,
        )
    )


def _append_gate_b_evidence(state: StateService, fixture: _GateFixture) -> None:
    denied = (
        "read_state_database",
        "write_state_database",
        "write_unleased_file",
        "write_shared_artifact",
        "traverse_to_unleased_file",
        "scratch_symlink_to_state",
        "connect_gateway_unix_socket",
        "connect_nonallowlisted_tcp",
        "read_secret_environment",
    )
    if fixture.defect == "terminal-before-attempts":
        _append_gate_terminal(state, fixture)
    for operation in denied:
        payload: dict[str, JsonValue] = {
            "operation": operation,
            "probe_id": f"authority-probe:{operation}",
            "reason_code": (
                "WRONG_REASON"
                if fixture.defect == "wrong-gate-reason" and operation == denied[0]
                else "SANDBOX_ENFORCED"
            ),
        }
        if fixture.defect != "missing-gate-policy" or operation != denied[0]:
            payload["policy_hash"] = fixture.policy_hash
        state.append_event(
            AppendEventCommand(
                "SandboxProbeDenied",
                "sandbox_adapter",
                fixture.run_id,
                None,
                payload,
            )
        )
    for operation in ("write_allowed_scratch", "read_allowed_view"):
        state.append_event(
            AppendEventCommand(
                "SandboxProbePassed",
                "sandbox_adapter",
                fixture.run_id,
                None,
                {
                    "operation": operation,
                    "policy_hash": fixture.policy_hash,
                    "probe_id": f"authority-probe:{operation}",
                },
            )
        )
    if fixture.defect not in {"missing-gate-terminal", "terminal-before-attempts"}:
        _append_gate_terminal(state, fixture)


def _append_epoch_history(state: StateService, start_epoch: int) -> str:
    initial_base = HASHES[1] if start_epoch == 0 else HASHES[5]
    state.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {"base_epoch": initial_base, "knowledge_epoch": 0, "project_id": "fixture"},
        )
    )
    previous_base = initial_base
    for knowledge_epoch in range(1, start_epoch + 1):
        next_base = (
            HASHES[1]
            if knowledge_epoch == start_epoch
            else sha256_json({"prior_epoch": knowledge_epoch})
        )
        state.append_event(
            AppendEventCommand(
                "KnowledgeDeltaPublished",
                "promotion_service",
                f"prior-run-{knowledge_epoch}",
                None,
                {
                    "base_epoch": next_base,
                    "delta_id": f"prior-delta-{knowledge_epoch}",
                    "knowledge_epoch": knowledge_epoch,
                    "previous_base_epoch": previous_base,
                    "previous_knowledge_epoch": knowledge_epoch - 1,
                    "publication_sequence": knowledge_epoch,
                },
            )
        )
        previous_base = next_base
    return previous_base


def _register_artifacts(
    project: Path,
    state: StateService,
    run_id: str,
    defect: str | None = None,
    start_epoch: int = 0,
) -> None:
    manifest_body = _append_run_evidence(state, run_id, defect, start_epoch)
    alignment_body = canonical_json(
        {
            "review_kind": "machine",
            "reviewer": "codex-alignment-auditor",
            "verdict": "aligned",
        }
    )
    root = project / ".aizim" / "artifacts" / run_id
    if defect == "symlinked-artifact-root":
        outside = project / "outside-artifacts"
        outside.mkdir()
        root.parent.mkdir(parents=True, exist_ok=True)
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir(parents=True)
    for name, body, media_type in (
        ("run-manifest.json", manifest_body, "application/json"),
        ("alignment-review.json", alignment_body, "application/json"),
    ):
        path = root / name
        path.write_bytes(body)
        register_artifact(
            state,
            run_id,
            NamedArtifact(
                name,
                sha256_bytes(body),
                PurePosixPath(path.relative_to(project).as_posix()),
                media_type,
                len(body),
            ),
        )
    state.append_event(
        AppendEventCommand(
            "FormalTraceSealed",
            "evaluation_artifacts",
            run_id,
            None,
            {"cutoff_kind": "evaluation_artifacts"},
        )
    )
    trace = build_formal_trace(state.query_events(run_id))
    if defect == "post-seal-event":
        state.append_event(
            AppendEventCommand(
                "ScheduleProposed",
                "research_conductor",
                run_id,
                None,
                {
                    "directive_id": "post-seal-directive",
                    "execution_id": "post-seal-execution",
                    "role": "proof_explorer",
                    "worker_id": "prover-a",
                },
            )
        )
    if defect in {"trace-deleted", "trace-reordered", "trace-duplicated", "trace-relabeled"}:
        trace = _tampered_trace(trace, defect)
    trace_body = formal_trace_bytes(trace)
    trace_hash = sha256_bytes(trace_body)
    for name, body, media_type in (
        ("formal-trace.jsonl", trace_body, "application/x-ndjson"),
        ("formal-trace.sha256", f"{trace_hash}\n".encode(), "text/plain"),
    ):
        path = root / name
        path.write_bytes(body)
        register_artifact(
            state,
            run_id,
            NamedArtifact(
                name,
                sha256_bytes(body),
                PurePosixPath(path.relative_to(project).as_posix()),
                media_type,
                len(body),
            ),
        )
    report: dict[str, JsonValue] = {
        "alignment_verdict": "aligned",
        "engineering_smoke_statement": (
            "This result is an engineering smoke test and is not evidence of open-problem, "
            "novelty, or general autonomous proving capability."
        ),
        "evaluation_verdict": "pass",
        "kernel_verdict": "pass",
        "manifest_hash": sha256_bytes(manifest_body),
        "participation_label": "formal_unassisted",
        "runtime_acceptance_scope": RUNTIME_ACCEPTANCE_SCOPE,
        "trace_hash": trace_hash,
    }
    if defect == "incomplete-report":
        del report["evaluation_verdict"]
        del report["participation_label"]
    if defect == "missing-runtime-scope":
        del report["runtime_acceptance_scope"]
    report_body = canonical_json(report)
    if defect == "missing-artifact":
        return
    path = root / "acceptance-report.json"
    path.write_bytes(report_body)
    register_artifact(
        state,
        run_id,
        NamedArtifact(
            path.name,
            sha256_bytes(report_body),
            PurePosixPath(path.relative_to(project).as_posix()),
            "application/json",
            len(report_body),
        ),
    )
    if defect == "corrupt-artifact":
        path.write_bytes(report_body + b"\n")


def _tampered_trace(
    trace: tuple[FormalTraceRecord, ...], defect: str
) -> tuple[FormalTraceRecord, ...]:
    changed = list(trace)
    index = next(index for index, record in enumerate(changed) if record.kind == "worker")
    if defect == "trace-deleted":
        changed.pop(index)
    elif defect == "trace-reordered":
        changed[index], changed[index + 1] = changed[index + 1], changed[index]
    elif defect == "trace-duplicated":
        changed.insert(index, changed[index])
    else:
        changed[index] = replace(changed[index], kind="directive")
    previous = "0" * 64
    result: list[FormalTraceRecord] = []
    for ordinal, record in enumerate(changed):
        unsigned: dict[str, JsonValue] = {
            "ordinal": ordinal,
            "sequence": record.sequence,
            "kind": record.kind,
            "source_event_id": record.source_event_id,
            "source_event_type": record.source_event_type,
            "payload_hash": record.payload_hash,
            "content_hashes": list(record.content_hashes),
            "previous_hash": previous,
        }
        updated = replace(
            record,
            ordinal=ordinal,
            previous_hash=previous,
            record_hash=sha256_json(unsigned),
        )
        result.append(updated)
        previous = updated.record_hash
    return tuple(result)


def _build_fixture(
    project: Path,
    defect: str | None = None,
    gates: tuple[_GateFixture, ...] | None = None,
    start_epoch: int = 0,
) -> None:
    with StateService(StateServiceConfig(project, "acceptance-fixture")) as state:
        assert _append_epoch_history(state, start_epoch) == HASHES[1]
        for gate in gates or (_GateFixture("security-gate-positive", HASHES[7], defect),):
            _append_gate_b_evidence(state, gate)
        run_start = start_epoch - 1 if defect == "stale-start-epoch" else start_epoch
        _register_artifacts(project, state, "real-run", defect, run_start)
        state.append_event(
            AppendEventCommand(
                "RunCreated",
                "research_conductor",
                "newer-fake-run",
                None,
                {"manifest": {"model_backend": "fake"}},
            )
        )


def _run_checker(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--project", str(project), "--run-id", "latest-real"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_checker_help_limits_16_of_16_to_replayable_runtime_evidence() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--help"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    output = " ".join(result.stdout.split())
    assert "replayable real-run predicates" in output
    assert "does not independently prove CI-only or test-only rows" in output


def test_operator_docs_preserve_runtime_and_overall_matrix_boundaries() -> None:
    for source in (REPOSITORY_ROOT / "README.md", FOUNDATION_RUNBOOK):
        body = " ".join(source.read_text().split())
        assert "replayable real-run predicates" in body
        assert "does not independently prove CI-only or test-only rows" in body
        assert "overall handoff matrix" in body
    recovery = RECOVERY_RUNBOOK.read_text()
    assert "umask 077" in recovery
    assert "${TMPDIR%/}/aizim-status." in recovery
    assert "/tmp/aizim-status.json" not in recovery


def test_complete_foundation_evidence_passes_for_latest_real_run(
    foundation_project: Path,
) -> None:
    # Given
    _build_fixture(foundation_project)

    # When
    result = _run_checker(foundation_project)

    # Then
    assert result.returncode == 0, result.stderr
    assert result.stdout == "FOUNDATION ACCEPTANCE PASS 16/16\n"


def test_complete_foundation_evidence_passes_from_nonzero_epoch(
    foundation_project: Path,
) -> None:
    # Given
    _build_fixture(foundation_project, start_epoch=4)

    # When
    result = _run_checker(foundation_project)

    # Then
    assert result.returncode == 0, result.stderr
    assert result.stdout == "FOUNDATION ACCEPTANCE PASS 16/16\n"


def test_complete_foundation_evidence_passes_from_a_long_project_path(
    foundation_project: Path,
) -> None:
    # Given
    project = foundation_project / ("nested-" + "x" * 80)
    project.mkdir()
    _build_fixture(project)

    # When
    result = _run_checker(project)

    # Then
    assert result.returncode == 0, result.stderr
    assert result.stdout == "FOUNDATION ACCEPTANCE PASS 16/16\n"


def test_foundation_evidence_rejects_a_stale_manifest_start_epoch(
    foundation_project: Path,
) -> None:
    # Given
    _build_fixture(foundation_project, "stale-start-epoch", start_epoch=4)

    # When
    result = _run_checker(foundation_project)

    # Then
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "FOUNDATION ACCEPTANCE FAIL\n"


def test_latest_failed_gate_group_does_not_fall_back_to_older_success(
    foundation_project: Path,
) -> None:
    # Given
    gates = (
        _GateFixture("security-gate-a", HASHES[7]),
        _GateFixture("security-gate-b", HASHES[6], "failed-gate-after-attempts"),
    )
    _build_fixture(foundation_project, gates=gates)

    # When
    result = _run_checker(foundation_project)

    # Then
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "FOUNDATION ACCEPTANCE FAIL\n"


def test_latest_incomplete_gate_group_does_not_fall_back_to_older_success(
    foundation_project: Path,
) -> None:
    # Given
    gates = (
        _GateFixture("security-gate-a", HASHES[7]),
        _GateFixture("security-gate-b", HASHES[6], "missing-gate-terminal"),
    )
    _build_fixture(foundation_project, gates=gates)

    # When
    result = _run_checker(foundation_project)

    # Then
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "FOUNDATION ACCEPTANCE FAIL\n"


def test_latest_valid_gate_group_supplies_real_completion_policy(
    foundation_project: Path,
) -> None:
    # Given
    gates = (
        _GateFixture("security-gate-a", HASHES[7]),
        _GateFixture("security-gate-b", HASHES[6]),
    )
    _build_fixture(foundation_project, "latest-valid-gate", gates)

    # When
    result = _run_checker(foundation_project)

    # Then
    assert result.returncode == 0, result.stderr
    assert result.stdout == "FOUNDATION ACCEPTANCE PASS 16/16\n"


@pytest.mark.parametrize(
    "defect",
    (
        "extra-verification",
        "missing-gate-policy",
        "wrong-gate-reason",
        "missing-local-loogle",
        "incomplete-report",
        "missing-runtime-scope",
        "missing-shutdown",
        "failed-run-outcome",
        "failed-gate-after-attempts",
        "terminal-before-attempts",
        "mismatched-completion-policy",
        "missing-source-scan",
        "failed-source-scan",
        "missing-artifact",
        "corrupt-artifact",
        "symlinked-artifact-root",
        "trace-deleted",
        "trace-reordered",
        "trace-duplicated",
        "trace-relabeled",
        "post-seal-event",
    ),
)
def test_incomplete_foundation_evidence_fails_closed(foundation_project: Path, defect: str) -> None:
    # Given
    _build_fixture(foundation_project, defect)

    # When
    result = _run_checker(foundation_project)

    # Then
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "FOUNDATION ACCEPTANCE FAIL\n"
