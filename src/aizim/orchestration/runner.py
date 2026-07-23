from __future__ import annotations

import asyncio
import secrets
import shutil
import sys
from pathlib import Path

from aizim.agents import BackendIdentity, CodexBackend
from aizim.config import AizimConfig, load_config
from aizim.config.model import LeanRuntimeMode
from aizim.domain import EpochPair, sha256_bytes
from aizim.lean.broker_knowledge import current_epoch
from aizim.lean.project import smoke_base_epoch
from aizim.modes.evaluation import EvaluationPolicy, ManifestInput
from aizim.modes.formal_trace import build_formal_trace, formal_trace_bytes
from aizim.modes.manifest import (
    acceptance_report_bytes,
    alignment_review_bytes,
    canonical_manifest,
    manifest_document,
    manifest_hash,
    register_artifact,
    write_named_artifact,
)
from aizim.runtime.layout import ProjectLayout
from aizim.state import AppendEventCommand, StateService, StateServiceConfig
from aizim.state.events import utc_now

from .codex_auditor import abort_for_alignment, audit_alignment, record_machine_alignment
from .codex_worker import codex_worker_factory, create_codex_backend
from .conductor import ResearchConductor, SharedRunResult
from .evaluation_contract import (
    ALLOWED_IMPORTS,
    PROOF_RUN_TOOLS,
    cache_state,
    environment_fingerprint,
)
from .resources import ResourceGovernor
from .run_failures import RunError, RunFailureCategory
from .run_failures import run_exit_code as _run_exit_code
from .run_reporting import print_run_summary, record_completion


def run_autonomous_shared(project: Path, backend: str) -> int:
    try:
        config, result = asyncio.run(_run(project, backend))
    except (Exception, asyncio.CancelledError, KeyboardInterrupt) as error:
        print("aizim run: autonomous-shared run failed", file=sys.stderr)
        return int(_run_exit_code(error))
    print_run_summary(config, result, backend)
    return 0


async def _run(project: Path, backend: str) -> tuple[AizimConfig, SharedRunResult]:
    if backend not in {"fake", "codex"}:
        raise RunError("BACKEND_UNAVAILABLE")
    layout = ProjectLayout.from_lean_project(project)
    layout.prepare_runtime()
    layout.validate_state_lock_entry()
    layout.validate_database_entry()
    config = load_config(layout.root)
    if config.run.lean_runtime is not LeanRuntimeMode.SHARED:
        raise RunError("ISOLATED_RUNTIME_UNAVAILABLE")
    state = StateService(StateServiceConfig(layout.root, "autonomous-shared"))
    try:
        layout.secure_database_permissions()
        _initialize(state, layout.root)
        governor = ResourceGovernor(
            config.resources, disk_free=lambda root: shutil.disk_usage(root).free
        )
        codex_backend: CodexBackend | None = None
        if backend == "fake":
            backend_identity = BackendIdentity("fake", "deterministic-v1", None)
            model_identifier = "deterministic-fixture"
            isolation_profile = "deterministic-in-process"
        else:
            if config.model is None:
                raise RunError("MODEL_REQUIRED")
            real_backend = create_codex_backend()
            codex_backend = real_backend
            backend_identity = real_backend.identity
            model_identifier = config.model
            isolation_profile = "macos-sandbox-aizim-worker"
        policy = EvaluationPolicy(config.run)
        run_id = f"shared-{secrets.token_hex(8)}"
        start_epoch = current_epoch(state)
        manifest_input = _manifest_input(
            config,
            start_epoch,
            layout.root,
            run_id,
            backend_identity,
            model_identifier,
            isolation_profile,
        )
        start_commitment = policy.manifest(manifest_input)
        state.append_event(
            AppendEventCommand(
                "RunCreated",
                "research_conductor",
                run_id,
                None,
                {"manifest": manifest_document(start_commitment), "status": "running"},
            )
        )
        conductor = ResearchConductor(state, layout.root, layout.root, governor)
        if backend == "fake":
            fixtures = _fixture_root()
            result = await conductor.run_fake(
                fixtures / "prover_a.json",
                fixtures / "prover_b.json",
                run_id,
                record_completion=False,
                start_epoch=start_epoch,
            )
            record_machine_alignment(state, run_id, "deterministic-auditor", "aligned")
            record_completion(state, run_id)
        else:
            assert codex_backend is not None and config.model is not None
            result = await conductor.run_two_worker(
                codex_worker_factory(codex_backend, layout.root, config.model),
                run_id,
                record_completion=False,
                start_epoch=start_epoch,
                prewarm_runtime=True,
            )
            try:
                verdict = await audit_alignment(
                    state, layout.root, run_id, codex_backend, config.model
                )
            except BaseException:
                abort_for_alignment(state, run_id)
                _record_evaluation_artifacts(state, layout.root, policy, result, manifest_input)
                raise
            if verdict != "aligned":
                abort_for_alignment(state, run_id)
                _record_evaluation_artifacts(state, layout.root, policy, result, manifest_input)
                raise RunError(
                    "ALIGNMENT_AUDIT_FAILED", RunFailureCategory.LEAN_VERIFICATION
                )
            record_completion(state, run_id)
        _record_evaluation_artifacts(state, layout.root, policy, result, manifest_input)
    finally:
        state.close()
    return config, result


def _initialize(state: StateService, project_root: Path) -> None:
    if any(record.envelope.event_type == "ProjectInitialized" for record in state.query_events()):
        return
    state.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {
                "project_id": project_root.name,
                "base_epoch": smoke_base_epoch(project_root),
                "knowledge_epoch": 0,
            },
        )
    )


def _fixture_root() -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures" / "fake_workers"


def _manifest_input(
    config: AizimConfig,
    start_epoch: EpochPair,
    project_root: Path,
    run_id: str,
    backend: BackendIdentity,
    model_identifier: str,
    isolation_profile: str,
) -> ManifestInput:
    prompt_root = Path(__file__).resolve().parents[1] / "agents" / "prompts"
    prompts = tuple(
        (name, sha256_bytes((prompt_root / f"{name}.md").read_bytes()))
        for name in ("proof_worker", "alignment_auditor")
    )
    return ManifestInput(
        run_id=run_id,
        epoch_pair=start_epoch,
        environment_fingerprint=environment_fingerprint(project_root),
        started_at=utc_now(),
        config=config,
        backend=backend,
        model_identifier=model_identifier,
        goal="prove two shared Lean smoke declarations",
        allowed_imports=ALLOWED_IMPORTS,
        tool_surface=tuple(tool.value for tool in PROOF_RUN_TOOLS),
        prompt_hashes=prompts,
        cache_state=cache_state(project_root),
        process_isolation_profile=isolation_profile,
    )


def _record_evaluation_artifacts(
    state: StateService,
    project_root: Path,
    policy: EvaluationPolicy,
    result: SharedRunResult,
    manifest_input: ManifestInput,
) -> None:
    events = state.query_events(result.run_id)
    labels = policy.labels(events)
    manifest = policy.evaluated_manifest(manifest_input, events)
    manifest_artifact = write_named_artifact(
        project_root,
        result.run_id,
        "run-manifest.json",
        canonical_manifest(manifest),
        "application/json",
    )
    register_artifact(state, result.run_id, manifest_artifact)
    alignment = write_named_artifact(
        project_root,
        result.run_id,
        "alignment-review.json",
        alignment_review_bytes(labels),
        "application/json",
    )
    register_artifact(state, result.run_id, alignment)
    state.append_event(
        AppendEventCommand(
            "FormalTraceSealed",
            "evaluation_artifacts",
            result.run_id,
            None,
            {"cutoff_kind": "evaluation_artifacts"},
        )
    )
    trace = build_formal_trace(state.query_events(result.run_id))
    trace_body = formal_trace_bytes(trace)
    trace_digest = sha256_bytes(trace_body)
    trace_artifact = write_named_artifact(
        project_root, result.run_id, "formal-trace.jsonl", trace_body, "application/x-ndjson"
    )
    trace_hash_artifact = write_named_artifact(
        project_root,
        result.run_id,
        "formal-trace.sha256",
        f"{trace_digest}\n".encode(),
        "text/plain",
    )
    register_artifact(state, result.run_id, trace_artifact)
    register_artifact(state, result.run_id, trace_hash_artifact)
    acceptance = write_named_artifact(
        project_root,
        result.run_id,
        "acceptance-report.json",
        acceptance_report_bytes(
            labels, result.verified_declarations, manifest_hash(manifest), trace_digest
        ),
        "application/json",
    )
    register_artifact(state, result.run_id, acceptance)
