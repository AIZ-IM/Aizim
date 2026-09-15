from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final, assert_never

if TYPE_CHECKING:
    from ..domain.model import EpochPair

LEAN_TOOLCHAIN: Final = "leanprover/lean4:v4.32.1"
LEAN_LSP_MCP_VERSION: Final = "0.28.1"
LEANCLIENT_VERSION: Final = "0.12.1"
MCP_VERSION: Final = "1.28.1"
CODEX_CLI_VERSION: Final = "0.154.0"
MIN_FREE_DISK_BYTES: Final = 2_147_483_648
PROOF_WORKER_TIMEOUT_SECONDS: Final = 60.0
ALIGNMENT_AUDITOR_TIMEOUT_SECONDS: Final = 60.0


@dataclass(frozen=True, slots=True)
class ConfigError(ValueError):
    location: str
    reason: str

    def __str__(self) -> str:
        return f"{self.location}: {self.reason}"


def _text(value: str, location: str) -> None:
    if type(value) is not str or not value:
        raise ConfigError(location, "must be a non-empty string")


def _sequence(value: int, location: str) -> None:
    if type(value) is not int or value < 0:
        raise ConfigError(location, "must be a non-negative integer")


def _strings(values: tuple[str, ...], location: str) -> None:
    if type(values) is not tuple or any(type(value) is not str for value in values):
        raise ConfigError(location, "must be a tuple of strings")


def _pairs(values: tuple[tuple[str, str], ...], location: str) -> None:
    valid = type(values) is tuple and all(
        type(pair) is tuple and len(pair) == 2 and all(type(value) is str for value in pair)
        for pair in values
    )
    if not valid:
        raise ConfigError(location, "must be a tuple of string pairs")


def _numeric_pairs(
    values: tuple[tuple[str, int], ...] | tuple[tuple[str, float], ...],
    numeric_type: type[int] | type[float],
    location: str,
) -> None:
    valid = type(values) is tuple and all(
        type(pair) is tuple
        and len(pair) == 2
        and type(pair[0]) is str
        and type(pair[1]) is numeric_type
        and pair[1] >= 0
        for pair in values
    )
    if not valid:
        raise ConfigError(location, "must contain non-negative numeric entries")


class ParticipationMode(StrEnum):
    AUTONOMOUS = "autonomous"
    COLLABORATIVE = "collaborative"
    LEARNING = "learning"


class FormalParticipation(StrEnum):
    UNASSISTED = "formal_unassisted"
    ASSISTED = "formal_assisted"
    EDUCATIONAL = "educational"


class AlignmentReviewKind(StrEnum):
    NONE = "none"
    MACHINE = "machine"
    HUMAN = "human"


class LeanRuntimeMode(StrEnum):
    SHARED = "shared"
    ISOLATED = "isolated"


@dataclass(frozen=True, slots=True)
class AlignmentReview:
    review_kind: AlignmentReviewKind = AlignmentReviewKind.NONE
    reviewer_identity: str | None = None
    protocol_revision: str | None = None
    verdict: str | None = None

    def __post_init__(self) -> None:
        if type(self.review_kind) is not AlignmentReviewKind:
            raise ConfigError("review_kind", "must be an AlignmentReviewKind")
        metadata = (self.reviewer_identity, self.protocol_revision, self.verdict)
        match self.review_kind:
            case AlignmentReviewKind.NONE:
                if any(value is not None for value in metadata):
                    raise ConfigError("alignment_review", "none cannot contain reviewer metadata")
            case AlignmentReviewKind.MACHINE | AlignmentReviewKind.HUMAN:
                for location, value in zip(
                    ("reviewer_identity", "protocol_revision", "verdict"), metadata, strict=True
                ):
                    if value is None:
                        raise ConfigError(location, "is required for a completed review")
                    _text(value, location)
            case unreachable:
                assert_never(unreachable)


@dataclass(frozen=True, slots=True)
class ResourcePolicy:
    max_proof_workers: int = 2
    max_question_workers: int = 3
    scratch_slots: int = 2
    lsp_instances: int = 1
    local_loogle: bool = False
    remote_search_max_concurrency: int = 1
    min_free_disk_bytes: int = MIN_FREE_DISK_BYTES

    def __post_init__(self) -> None:
        for location, value in (
            ("max_proof_workers", self.max_proof_workers),
            ("max_question_workers", self.max_question_workers),
            ("scratch_slots", self.scratch_slots),
            ("lsp_instances", self.lsp_instances),
            ("remote_search_max_concurrency", self.remote_search_max_concurrency),
            ("min_free_disk_bytes", self.min_free_disk_bytes),
        ):
            _sequence(value, location)
        if type(self.local_loogle) is not bool:
            raise ConfigError("local_loogle", "must be a boolean")
        if self.min_free_disk_bytes != MIN_FREE_DISK_BYTES:
            raise ConfigError("min_free_disk_bytes", "must equal the fixed 2 GiB floor")


@dataclass(frozen=True, slots=True)
class RunPolicy:
    participation: ParticipationMode = ParticipationMode.AUTONOMOUS
    formal_participation: FormalParticipation = FormalParticipation.UNASSISTED
    lean_runtime: LeanRuntimeMode = LeanRuntimeMode.SHARED
    human_interventions: int = 0
    environment_frozen: bool = True

    def __post_init__(self) -> None:
        if type(self.participation) is not ParticipationMode:
            raise ConfigError("participation", "must be a ParticipationMode")
        if type(self.formal_participation) is not FormalParticipation:
            raise ConfigError("formal_participation", "must be a FormalParticipation")
        if type(self.lean_runtime) is not LeanRuntimeMode:
            raise ConfigError("lean_runtime", "must be a LeanRuntimeMode")
        _sequence(self.human_interventions, "human_interventions")
        if type(self.environment_frozen) is not bool:
            raise ConfigError("environment_frozen", "must be a boolean")
        if self.formal_participation is FormalParticipation.UNASSISTED and (
            self.participation is not ParticipationMode.AUTONOMOUS
            or self.human_interventions != 0
            or not self.environment_frozen
        ):
            raise ConfigError(
                "formal_participation",
                "formal_unassisted requires autonomous, zero interventions, and frozen environment",
            )


@dataclass(frozen=True, slots=True)
class RunManifest:
    run_id: str
    epoch_pair: EpochPair
    event_schema_version: int
    environment_fingerprint: str
    started_at: datetime
    run_policy: RunPolicy
    resources: ResourcePolicy
    alignment_review: AlignmentReview
    lean_version: str
    toolchain_version: str
    dependency_versions: tuple[tuple[str, str], ...]
    lean_lsp_mcp_version: str
    leanclient_version: str
    agent_harness_name: str
    agent_harness_version: str
    agent_harness_binary_hash: str
    model_backend: str
    model_identifier: str
    prompts: tuple[tuple[str, str], ...]
    reasoning_settings: tuple[tuple[str, str], ...]
    budgets: tuple[tuple[str, int], ...]
    timeouts_seconds: tuple[tuple[str, float], ...]
    search_permissions: tuple[str, ...]
    capability_profile: str
    process_isolation_profile: str
    cache_state: str
    environment_transition_policy: str
    mathlib_version: str | None = None
    repl_revision: str | None = None
    rejected_transition_requests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        from ..domain.model import EpochPair

        if type(self.epoch_pair) is not EpochPair:
            raise ConfigError("epoch_pair", "must be an EpochPair")
        for location, value in (
            ("run_id", self.run_id),
            ("lean_version", self.lean_version),
            ("toolchain_version", self.toolchain_version),
            ("lean_lsp_mcp_version", self.lean_lsp_mcp_version),
            ("leanclient_version", self.leanclient_version),
            ("agent_harness_name", self.agent_harness_name),
            ("agent_harness_version", self.agent_harness_version),
            ("model_backend", self.model_backend),
            ("model_identifier", self.model_identifier),
            ("capability_profile", self.capability_profile),
            ("process_isolation_profile", self.process_isolation_profile),
            ("environment_transition_policy", self.environment_transition_policy),
        ):
            _text(value, location)
        _sequence(self.event_schema_version, "event_schema_version")
        if re.fullmatch(r"[0-9a-f]{64}", self.environment_fingerprint) is None:
            raise ConfigError("environment_fingerprint", "must be a lowercase SHA-256 hash")
        if re.fullmatch(r"[0-9a-f]{64}", self.agent_harness_binary_hash) is None:
            raise ConfigError("agent_harness_binary_hash", "must be a lowercase SHA-256 hash")
        if type(self.started_at) is not datetime or self.started_at.tzinfo is None:
            raise ConfigError("started_at", "must be a timezone-aware UTC datetime")
        if self.started_at.utcoffset() != timedelta(0):
            raise ConfigError("started_at", "must be a timezone-aware UTC datetime")
        if type(self.run_policy) is not RunPolicy:
            raise ConfigError("run_policy", "must be a RunPolicy")
        if type(self.resources) is not ResourcePolicy:
            raise ConfigError("resources", "must be a ResourcePolicy")
        if type(self.alignment_review) is not AlignmentReview:
            raise ConfigError("alignment_review", "must be an AlignmentReview")
        for location, values in (
            ("dependency_versions", self.dependency_versions),
            ("prompts", self.prompts),
            ("reasoning_settings", self.reasoning_settings),
        ):
            _pairs(values, location)
        _numeric_pairs(self.budgets, int, "budgets")
        _numeric_pairs(self.timeouts_seconds, float, "timeouts_seconds")
        _strings(self.search_permissions, "search_permissions")
        _strings(self.rejected_transition_requests, "rejected_transition_requests")
        if self.cache_state not in {"warm", "cold"}:
            raise ConfigError("cache_state", "must be warm or cold")
        for location, value in (
            ("mathlib_version", self.mathlib_version),
            ("repl_revision", self.repl_revision),
        ):
            if value is not None:
                _text(value, location)


@dataclass(frozen=True, slots=True)
class AizimConfig:
    run: RunPolicy = field(default_factory=RunPolicy)
    resources: ResourcePolicy = field(default_factory=ResourcePolicy)
    model: str | None = None
    lean_toolchain: str = LEAN_TOOLCHAIN
    lean_lsp_mcp_version: str = LEAN_LSP_MCP_VERSION
    leanclient_version: str = LEANCLIENT_VERSION
    mcp_version: str = MCP_VERSION
    codex_cli_version: str = CODEX_CLI_VERSION

    def __post_init__(self) -> None:
        if type(self.run) is not RunPolicy or type(self.resources) is not ResourcePolicy:
            raise ConfigError("config", "run and resources must be typed policy records")
        if self.model is not None:
            _text(self.model, "model")
        for location, actual, required in (
            ("lean_toolchain", self.lean_toolchain, LEAN_TOOLCHAIN),
            ("lean_lsp_mcp_version", self.lean_lsp_mcp_version, LEAN_LSP_MCP_VERSION),
            ("leanclient_version", self.leanclient_version, LEANCLIENT_VERSION),
            ("mcp_version", self.mcp_version, MCP_VERSION),
            ("codex_cli_version", self.codex_cli_version, CODEX_CLI_VERSION),
        ):
            if actual != required:
                raise ConfigError(location, f"must equal exact foundation pin {required}")
