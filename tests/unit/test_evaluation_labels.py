from __future__ import annotations

import json
from pathlib import Path

from aizim.config.model import (
    AlignmentReviewKind,
    FormalParticipation,
    ParticipationMode,
    RunPolicy,
)
from aizim.modes.evaluation import EvaluationPolicy
from aizim.modes.manifest import acceptance_report_bytes
from aizim.state import AppendEventCommand, StateService, StateServiceConfig


def _service(tmp_path: Path) -> StateService:
    return StateService(StateServiceConfig(tmp_path, "evaluation-labels"))


def test_autonomous_labels_freeze_imports_and_reject_post_start_changes(tmp_path: Path) -> None:
    policy = EvaluationPolicy(RunPolicy())

    labels = policy.labels(())
    rejection = policy.reject_transition("model", {"identifier": "replacement"})

    assert labels.formal_label is FormalParticipation.UNASSISTED
    assert labels.environment_frozen and labels.imports_frozen and labels.scored
    assert rejection.field == "model"
    with _service(tmp_path) as state:
        policy.record_rejection(state, "run-1", rejection)
        event = state.query_events("run-1")[0].envelope
    assert event.event_type == "EvaluationTransitionRejected"
    assert event.payload["field"] == "model"


def test_collaborative_and_learning_modes_receive_their_required_formal_labels() -> None:
    collaborative = EvaluationPolicy(
        RunPolicy(
            participation=ParticipationMode.COLLABORATIVE,
            formal_participation=FormalParticipation.ASSISTED,
            environment_frozen=False,
        )
    )
    learning = EvaluationPolicy(
        RunPolicy(
            participation=ParticipationMode.LEARNING,
            formal_participation=FormalParticipation.EDUCATIONAL,
            environment_frozen=False,
        )
    )

    assert collaborative.labels(()).formal_label is FormalParticipation.ASSISTED
    assert learning.labels(()).formal_label is FormalParticipation.EDUCATIONAL


def test_human_intervention_aborts_autonomous_scoring_and_elevates_formal_label(
    tmp_path: Path,
) -> None:
    policy = EvaluationPolicy(RunPolicy())
    with _service(tmp_path) as state:
        state.append_event(
            AppendEventCommand(
                "InterventionRecorded",
                "human_operator",
                "run-1",
                None,
                {
                    "intervention_id": "human-1",
                    "kind": "human",
                    "actor": "operator",
                    "reason": "help",
                },
            )
        )
        labels = policy.labels(state.query_events("run-1"))

    assert labels.human_interventions == 1
    assert labels.formal_label is FormalParticipation.ASSISTED
    assert labels.terminal_status == "aborted"
    assert not labels.scored


def test_machine_review_is_not_a_human_intervention_or_semantic_human_review(
    tmp_path: Path,
) -> None:
    policy = EvaluationPolicy(RunPolicy())
    with _service(tmp_path) as state:
        state.append_event(
            AppendEventCommand(
                "AlignmentReviewed",
                "alignment_auditor",
                "run-1",
                None,
                {
                    "review_id": "machine-1",
                    "kind": "machine",
                    "reviewer": "codex-auditor",
                    "verdict": "aligned",
                },
            )
        )
        labels = policy.labels(state.query_events("run-1"))

    assert labels.alignment_kind is AlignmentReviewKind.MACHINE
    assert labels.human_interventions == 0
    assert labels.formal_label is FormalParticipation.UNASSISTED


def test_human_review_requires_a_matching_explicit_human_intervention(tmp_path: Path) -> None:
    policy = EvaluationPolicy(RunPolicy())
    with _service(tmp_path) as state:
        state.append_event(
            AppendEventCommand(
                "AlignmentReviewed",
                "untrusted_actor",
                "run-1",
                None,
                {
                    "review_id": "claimed-human",
                    "kind": "human",
                    "reviewer": "x",
                    "verdict": "aligned",
                },
            )
        )
        without_human_event = policy.labels(state.query_events("run-1"))
        state.append_event(
            AppendEventCommand(
                "InterventionRecorded",
                "human_operator",
                "run-1",
                None,
                {
                    "intervention_id": "human-1",
                    "kind": "human",
                    "actor": "operator",
                    "reason": "review",
                },
            )
        )
        with_human_event = policy.labels(state.query_events("run-1"))

    assert without_human_event.alignment_kind is AlignmentReviewKind.NONE
    assert with_human_event.alignment_kind is AlignmentReviewKind.HUMAN


def test_emergency_stop_cannot_be_reported_as_a_scored_success(tmp_path: Path) -> None:
    policy = EvaluationPolicy(RunPolicy())
    with _service(tmp_path) as state:
        state.append_event(
            AppendEventCommand(
                "RunAborted",
                "operator",
                "run-1",
                None,
                {"reason_code": "EMERGENCY_STOP"},
            )
        )
        labels = policy.labels(state.query_events("run-1"))

    assert labels.terminal_status == "aborted"
    assert not labels.scored


def test_alignment_abort_does_not_rewrite_kernel_verdict(tmp_path: Path) -> None:
    policy = EvaluationPolicy(RunPolicy())
    with _service(tmp_path) as state:
        state.append_event(
            AppendEventCommand(
                "RunAborted",
                "evaluation_policy",
                "run-1",
                None,
                {"reason_code": "ALIGNMENT_AUDIT_FAILED"},
            )
        )
        labels = policy.labels(state.query_events("run-1"))

    report = json.loads(acceptance_report_bytes(labels, 2, "a" * 64, "b" * 64))

    assert report["kernel_verdict"] == "pass"
    assert report["evaluation_verdict"] == "aborted"
