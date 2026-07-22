from __future__ import annotations

from typing import Final

ARTIFACT_NAMES: Final = frozenset(
    {
        "acceptance-report.json",
        "alignment-review.json",
        "formal-trace.jsonl",
        "formal-trace.sha256",
        "run-manifest.json",
    }
)
DENIED_OPERATIONS: Final = frozenset(
    {
        "connect_gateway_unix_socket",
        "connect_nonallowlisted_tcp",
        "read_secret_environment",
        "read_state_database",
        "scratch_symlink_to_state",
        "traverse_to_unleased_file",
        "write_shared_artifact",
        "write_state_database",
        "write_unleased_file",
    }
)
ALLOWED_OPERATIONS: Final = frozenset({"read_allowed_view", "write_allowed_scratch"})
STABLE_MANIFEST_FIELDS: Final = (
    "agent_harness_binary_hash",
    "agent_harness_name",
    "agent_harness_version",
    "budgets",
    "cache_state",
    "capability_profile",
    "dependency_versions",
    "environment_fingerprint",
    "environment_transition_policy",
    "epoch_pair",
    "event_schema_version",
    "lean_lsp_mcp_version",
    "lean_version",
    "leanclient_version",
    "mathlib_version",
    "model_backend",
    "model_identifier",
    "process_isolation_profile",
    "prompts",
    "reasoning_settings",
    "repl_revision",
    "resources",
    "run_id",
    "run_policy",
    "search_permissions",
    "started_at",
    "timeouts_seconds",
    "toolchain_version",
)
FAILURE_EVENT_TYPES: Final = frozenset(
    {
        "EvaluationTransitionRejected",
        "InterventionRecorded",
        "LeanRuntimeCrashed",
        "PromotionFailed",
        "RunAborted",
        "WorkerCrashed",
        "WorkerTimedOut",
    }
)
PROOF_EXECUTIONS: Final = ("prover-a", "prover-b", "prover-b")
REAL_COMPLETIONS: Final = ("alignment-auditor", *PROOF_EXECUTIONS)
