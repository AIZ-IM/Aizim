from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aizim.config import (
    CODEX_CLI_VERSION,
    LEAN_LSP_MCP_VERSION,
    LEAN_TOOLCHAIN,
    LEANCLIENT_VERSION,
    MCP_VERSION,
    AizimConfig,
    load_config,
)
from aizim.domain import (
    AlignmentReview,
    AlignmentReviewKind,
    EpochPair,
    FormalParticipation,
    LeanRuntimeMode,
    ParticipationMode,
    ResourcePolicy,
    RunManifest,
    RunPolicy,
)


def write_config(project_root: Path, body: str) -> None:
    config_dir = project_root / ".aizim"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(body)


@dataclass(frozen=True, slots=True)
class InvalidEpochPair:
    base_epoch: str
    knowledge_epoch: int


def run_manifest() -> RunManifest:
    return RunManifest(
        run_id="run-1",
        epoch_pair=EpochPair(base_epoch="a" * 64, knowledge_epoch=3),
        event_schema_version=1,
        environment_fingerprint="f" * 64,
        started_at=datetime(2026, 7, 21, 10, tzinfo=UTC),
        run_policy=RunPolicy(),
        resources=ResourcePolicy(),
        alignment_review=AlignmentReview(),
        lean_version="4.32.0",
        toolchain_version="leanprover/lean4:v4.32.0",
        dependency_versions=(("mcp", "1.28.1"),),
        lean_lsp_mcp_version="0.28.1",
        leanclient_version="0.12.1",
        agent_harness_name="codex-cli",
        agent_harness_version="0.144.6",
        agent_harness_binary_hash="1" * 64,
        model_backend="openai",
        model_identifier="gpt-5",
        prompts=(("proof", "proof-v1"),),
        reasoning_settings=(("effort", "high"),),
        budgets=(("tokens", 10_000),),
        timeouts_seconds=(("run", 300.0),),
        search_permissions=("remote_search",),
        capability_profile="proof-worker",
        process_isolation_profile="macos-seatbelt",
        cache_state="cold",
        environment_transition_policy="reject",
    )


def test_missing_config_uses_design_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path, environ={})

    assert config.model is None
    assert config.run.participation is ParticipationMode.AUTONOMOUS
    assert config.run.formal_participation is FormalParticipation.UNASSISTED
    assert config.run.lean_runtime is LeanRuntimeMode.SHARED
    assert config.run.environment_frozen
    assert config.resources.max_proof_workers == 2
    assert config.resources.max_question_workers == 3


def test_config_record_constructor_uses_design_defaults(tmp_path: Path) -> None:
    config = AizimConfig()

    assert config.run == load_config(tmp_path, environ={}).run
    assert config.resources.max_proof_workers == 2


@pytest.mark.parametrize(
    ("participation", "human_interventions", "environment_frozen"),
    [
        (ParticipationMode.COLLABORATIVE, 0, True),
        (ParticipationMode.AUTONOMOUS, 1, True),
        (ParticipationMode.AUTONOMOUS, 0, False),
    ],
)
def test_formal_unassisted_requires_autonomy_without_intervention_in_frozen_environment(
    participation: ParticipationMode,
    human_interventions: int,
    environment_frozen: bool,
) -> None:
    with pytest.raises(ValueError):
        RunPolicy(
            participation=participation,
            formal_participation=FormalParticipation.UNASSISTED,
            lean_runtime=LeanRuntimeMode.SHARED,
            human_interventions=human_interventions,
            environment_frozen=environment_frozen,
        )


def test_default_resource_policy_matches_current_mac_budget() -> None:
    policy = ResourcePolicy()

    assert policy.max_proof_workers == 2
    assert policy.max_question_workers == 3
    assert policy.scratch_slots == 2
    assert policy.lsp_instances == 1
    assert not policy.local_loogle
    assert policy.remote_search_max_concurrency == 1
    assert policy.min_free_disk_bytes == 2_147_483_648


def test_foundation_operating_profile_is_fixed_and_loadable(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        (
            "[foundation]\n"
            "schema_version = 1\n"
            'platform_adapter = "macos"\n'
            "repl_enabled = false\n"
            "remote_search_enabled = false\n"
        ),
    )

    config = load_config(tmp_path, environ={})

    assert config.resources.min_free_disk_bytes == 2_147_483_648


def test_disk_floor_cannot_be_disabled() -> None:
    with pytest.raises(ValueError):
        ResourcePolicy(min_free_disk_bytes=0)


def test_alignment_review_requires_reviewer_metadata() -> None:
    with pytest.raises(ValueError):
        AlignmentReview(
            review_kind=AlignmentReviewKind.MACHINE,
            reviewer_identity=None,
            protocol_revision="alignment-v1",
            verdict="aligned",
        )


def test_run_manifest_records_evaluation_boundary() -> None:
    manifest = run_manifest()

    assert manifest.lean_version == "4.32.0"
    assert manifest.model_identifier == "gpt-5"


def test_run_manifest_rejects_noncanonical_epoch_record() -> None:
    # Given
    invalid_epoch = InvalidEpochPair(base_epoch="a" * 64, knowledge_epoch=3)

    # When / Then
    with pytest.raises(ValueError):
        replace(run_manifest(), epoch_pair=invalid_epoch)


def test_config_reads_toml_and_environment_model_override(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        (
            'model = "file-model"\n\n'
            "[run]\n"
            'participation = "collaborative"\n'
            'lean_runtime = "isolated"\n'
            "human_interventions = 2\n\n"
            "[resources]\n"
            "max_proof_workers = 1\n"
            "max_question_workers = 2\n"
            "scratch_slots = 1\n"
            "lsp_instances = 1\n"
            "local_loogle = false\n"
            "remote_search_max_concurrency = 1\n\n"
            "[foundation]\n"
            f'lean_toolchain = "{LEAN_TOOLCHAIN}"\n'
            f'lean_lsp_mcp_version = "{LEAN_LSP_MCP_VERSION}"\n'
            f'leanclient_version = "{LEANCLIENT_VERSION}"\n'
            f'mcp_version = "{MCP_VERSION}"\n'
            f'codex_cli_version = "{CODEX_CLI_VERSION}"\n'
        ),
    )

    config = load_config(tmp_path, environ={"AIZIM_MODEL": "environment-model"})

    assert config.model == "environment-model"
    assert config.run.participation is ParticipationMode.COLLABORATIVE
    assert config.run.formal_participation is FormalParticipation.ASSISTED
    assert config.run.lean_runtime is LeanRuntimeMode.ISOLATED
    assert config.run.human_interventions == 2
    assert config.resources.max_proof_workers == 1


@pytest.mark.parametrize(
    "body",
    [
        "unexpected = true\n",
        "[run]\nunexpected = true\n",
        "[resources]\nunexpected = true\n",
        "[foundation]\nunexpected = true\n",
    ],
)
def test_unknown_config_keys_are_fatal(tmp_path: Path, body: str) -> None:
    write_config(tmp_path, body)

    with pytest.raises(ValueError):
        load_config(tmp_path, environ={})


def test_unknown_enum_value_is_fatal(tmp_path: Path) -> None:
    write_config(tmp_path, '[run]\nparticipation = "observer"\n')

    with pytest.raises(ValueError):
        load_config(tmp_path, environ={})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("lean_toolchain", "leanprover/lean4:v4.31.0"),
        ("lean_lsp_mcp_version", "0.28.0"),
        ("leanclient_version", "0.12.0"),
        ("mcp_version", "1.28.0"),
        ("codex_cli_version", "0.144.5"),
    ],
)
def test_foundation_pins_must_match_exact_versions(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    write_config(tmp_path, f'[foundation]\n{key} = "{value}"\n')

    with pytest.raises(ValueError):
        load_config(tmp_path, environ={})


def test_aizim_config_never_represents_secrets() -> None:
    names = {field.name.lower() for field in fields(AizimConfig)}

    secret_fragments = ("secret", "token", "api_key")
    assert not any(fragment in name for name in names for fragment in secret_fragments)
