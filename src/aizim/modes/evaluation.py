from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from aizim.agents import BackendIdentity
from aizim.config import AizimConfig
from aizim.config.model import (
    ALIGNMENT_AUDITOR_TIMEOUT_SECONDS,
    PROOF_WORKER_TIMEOUT_SECONDS,
    AlignmentReview,
    AlignmentReviewKind,
    FormalParticipation,
    ParticipationMode,
    RunManifest,
    RunPolicy,
)
from aizim.domain import EpochPair, sha256_json
from aizim.state import AppendEventCommand, StateService
from aizim.state.store_contracts import EventRecord

_FROZEN_FIELDS: Final = frozenset(
    {
        "goal",
        "budget",
        "model",
        "worker_count",
        "runtime_mode",
        "tool_surface",
        "allowed_imports",
        "environment_fingerprint",
        "starting_epoch_pair",
        "prompts",
    }
)


@dataclass(frozen=True, slots=True)
class RejectedTransition:
    field: str
    requested_hash: str
    reason_code: str = "POST_START_MUTATION"

    def __post_init__(self) -> None:
        if self.field not in _FROZEN_FIELDS or len(self.requested_hash) != 64:
            raise ValueError("INVALID_EVALUATION_TRANSITION")

    @property
    def manifest_entry(self) -> str:
        return f"{self.field}:{self.requested_hash}"


@dataclass(frozen=True, slots=True)
class EvaluationLabels:
    formal_label: FormalParticipation
    alignment_kind: AlignmentReviewKind
    alignment_verdict: str | None
    alignment_reviewer: str | None
    human_interventions: int
    environment_frozen: bool
    imports_frozen: bool
    scored: bool
    terminal_status: str


@dataclass(frozen=True, slots=True)
class ManifestInput:
    run_id: str
    epoch_pair: EpochPair
    environment_fingerprint: str
    started_at: datetime
    config: AizimConfig
    backend: BackendIdentity
    model_identifier: str
    goal: str
    allowed_imports: tuple[str, ...]
    tool_surface: tuple[str, ...]
    prompt_hashes: tuple[tuple[str, str], ...]
    cache_state: str
    process_isolation_profile: str


class EvaluationPolicy:
    def __init__(self, run_policy: RunPolicy) -> None:
        if type(run_policy) is not RunPolicy:
            raise ValueError("INVALID_EVALUATION_POLICY")
        self._run_policy = run_policy

    def reject_transition(self, field: str, requested: object) -> RejectedTransition:
        if field not in _FROZEN_FIELDS:
            raise ValueError("UNPROTECTED_EVALUATION_FIELD")
        return RejectedTransition(field, sha256_json(requested))

    def record_rejection(
        self, state: StateService, run_id: str, rejection: RejectedTransition
    ) -> None:
        if type(state) is not StateService or type(rejection) is not RejectedTransition:
            raise ValueError("INVALID_EVALUATION_REJECTION")
        state.append_event(
            AppendEventCommand(
                "EvaluationTransitionRejected",
                "evaluation_policy",
                run_id,
                None,
                {
                    "field": rejection.field,
                    "requested_hash": rejection.requested_hash,
                    "reason_code": rejection.reason_code,
                },
            )
        )

    def labels(self, events: tuple[EventRecord, ...]) -> EvaluationLabels:
        human_interventions = sum(
            record.envelope.event_type == "InterventionRecorded"
            and record.envelope.payload.get("kind") == "human"
            for record in events
        )
        aborted = any(record.envelope.event_type == "RunAborted" for record in events)
        review_kind, verdict, reviewer = _alignment(events, human_interventions)
        autonomous = self._run_policy.participation is ParticipationMode.AUTONOMOUS
        unassisted = self._run_policy.formal_participation is FormalParticipation.UNASSISTED
        human_abort = autonomous and human_interventions > 0
        formal_label = (
            FormalParticipation.ASSISTED if human_abort else self._run_policy.formal_participation
        )
        terminal_status = _terminal_status(events, human_abort)
        return EvaluationLabels(
            formal_label,
            review_kind,
            verdict,
            reviewer,
            human_interventions,
            self._run_policy.environment_frozen,
            autonomous and self._run_policy.environment_frozen,
            unassisted and not human_interventions and not aborted,
            terminal_status,
        )

    def manifest(
        self, manifest_input: ManifestInput, rejections: tuple[RejectedTransition, ...] = ()
    ) -> RunManifest:
        return build_manifest(manifest_input, self.labels(()), rejections)

    def evaluated_manifest(
        self, manifest_input: ManifestInput, events: tuple[EventRecord, ...]
    ) -> RunManifest:
        rejections = tuple(
            RejectedTransition(
                str(record.envelope.payload["field"]),
                str(record.envelope.payload["requested_hash"]),
                str(record.envelope.payload["reason_code"]),
            )
            for record in events
            if record.envelope.event_type == "EvaluationTransitionRejected"
        )
        return build_manifest(manifest_input, self.labels(events), rejections)


def _alignment(
    events: tuple[EventRecord, ...], human_interventions: int
) -> tuple[AlignmentReviewKind, str | None, str | None]:
    reviews = [
        record.envelope.payload
        for record in events
        if record.envelope.event_type == "AlignmentReviewed"
    ]
    if not reviews:
        return AlignmentReviewKind.NONE, None, None
    review = reviews[-1]
    kind, verdict, reviewer = review.get("kind"), review.get("verdict"), review.get("reviewer")
    if kind == AlignmentReviewKind.MACHINE.value:
        return AlignmentReviewKind.MACHINE, _text_or_none(verdict), _text_or_none(reviewer)
    if kind == AlignmentReviewKind.HUMAN.value and human_interventions:
        return AlignmentReviewKind.HUMAN, _text_or_none(verdict), _text_or_none(reviewer)
    return AlignmentReviewKind.NONE, None, None


def _terminal_status(events: tuple[EventRecord, ...], human_abort: bool) -> str:
    if human_abort or any(record.envelope.event_type == "RunAborted" for record in events):
        return "aborted"
    if any(record.envelope.event_type == "RunCompleted" for record in events):
        return "pass"
    return "running"


def _text_or_none(value: object) -> str | None:
    return value if type(value) is str and value else None


def build_manifest(
    manifest_input: ManifestInput,
    labels: EvaluationLabels,
    rejections: tuple[RejectedTransition, ...],
) -> RunManifest:
    if type(manifest_input) is not ManifestInput or type(labels) is not EvaluationLabels:
        raise ValueError("INVALID_MANIFEST_INPUT")
    config, backend = manifest_input.config, manifest_input.backend
    return RunManifest(
        run_id=manifest_input.run_id,
        epoch_pair=manifest_input.epoch_pair,
        event_schema_version=1,
        environment_fingerprint=manifest_input.environment_fingerprint,
        started_at=manifest_input.started_at,
        run_policy=config.run,
        resources=config.resources,
        alignment_review=_alignment_review(labels),
        lean_version=config.lean_toolchain.removeprefix("leanprover/lean4:v"),
        toolchain_version=config.lean_toolchain,
        dependency_versions=(
            ("mcp", config.mcp_version),
            ("lean-lsp-mcp", config.lean_lsp_mcp_version),
            ("leanclient", config.leanclient_version),
        ),
        lean_lsp_mcp_version=config.lean_lsp_mcp_version,
        leanclient_version=config.leanclient_version,
        agent_harness_name=backend.name,
        agent_harness_version=backend.version,
        agent_harness_binary_hash=backend.executable_sha256
        or sha256_json({"name": backend.name, "version": backend.version}),
        model_backend=backend.name,
        model_identifier=manifest_input.model_identifier,
        prompts=manifest_input.prompt_hashes,
        reasoning_settings=(
            ("allowed_imports_hash", sha256_json(manifest_input.allowed_imports)),
            ("goal_hash", sha256_json(manifest_input.goal)),
            ("tool_surface_hash", sha256_json(manifest_input.tool_surface)),
        ),
        budgets=(
            ("proof_worker_actions", 12),
            ("proof_workers", config.resources.max_proof_workers),
        ),
        timeouts_seconds=(
            ("alignment_auditor", ALIGNMENT_AUDITOR_TIMEOUT_SECONDS),
            ("proof_worker", PROOF_WORKER_TIMEOUT_SECONDS),
        ),
        search_permissions=(),
        capability_profile=f"gateway-{sha256_json(manifest_input.tool_surface)[:16]}",
        process_isolation_profile=manifest_input.process_isolation_profile,
        cache_state=manifest_input.cache_state,
        environment_transition_policy="reject",
        rejected_transition_requests=tuple(sorted(item.manifest_entry for item in rejections)),
    )


def _alignment_review(labels: EvaluationLabels) -> AlignmentReview:
    if labels.alignment_kind is AlignmentReviewKind.NONE:
        return AlignmentReview()
    return AlignmentReview(
        labels.alignment_kind,
        labels.alignment_reviewer or "evaluation-auditor",
        "alignment-v1",
        labels.alignment_verdict or "pending",
    )
