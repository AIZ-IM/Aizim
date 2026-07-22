from __future__ import annotations

from inspect import signature
from pathlib import Path

import pytest

from aizim.domain import FormalParticipation, ParticipationMode, RunPolicy
from aizim.knowledge.environment import (
    EnvironmentPolicy,
    EnvironmentPolicyError,
    EnvironmentTransition,
    EnvironmentTransitionService,
)
from aizim.state import (
    AppendEventCommand,
    EventValidationError,
    StateDependencies,
    StateService,
    StateServiceConfig,
)

_OLD = "a" * 64
_NEW = "b" * 64
_OTHER = "c" * 64


def _transition() -> EnvironmentTransition:
    return EnvironmentTransition("transition-1", _OLD, _NEW, "new import policy")


def test_scored_formal_run_rejects_every_environment_transition() -> None:
    policy = EnvironmentPolicy(frozenset({_NEW}))

    with pytest.raises(EnvironmentPolicyError, match="ENVIRONMENT_FROZEN"):
        policy.authorize(RunPolicy(), _transition())


def test_unscored_autonomous_run_accepts_only_predeclared_transition() -> None:
    run = RunPolicy(
        participation=ParticipationMode.AUTONOMOUS,
        formal_participation=FormalParticipation.ASSISTED,
        environment_frozen=False,
    )
    policy = EnvironmentPolicy(frozenset({_NEW}))

    decision = policy.authorize(run, _transition())

    assert decision.approved
    assert decision.formal_participation is FormalParticipation.ASSISTED


def test_collaborative_transition_requires_a_durable_human_approval(tmp_path: Path) -> None:
    run = RunPolicy(
        participation=ParticipationMode.COLLABORATIVE,
        formal_participation=FormalParticipation.ASSISTED,
        environment_frozen=False,
    )
    policy = EnvironmentPolicy(frozenset())

    with pytest.raises(EnvironmentPolicyError, match="HUMAN_APPROVAL_REQUIRED"):
        policy.authorize(run, _transition())

    state = StateService(
        StateServiceConfig(tmp_path, "environment-policy"),
        StateDependencies(human_approval=lambda _run, reviewer: reviewer == "human-reviewer"),
    )
    try:
        service = EnvironmentTransitionService(state, policy)
        proposal = service.propose("run-1", "research_conductor", _transition())
        service.approve("run-1", "human-reviewer", proposal, _transition())
        decision = service.authorize("run-1", run, _transition())

        assert decision.approved
        assert decision.formal_participation is FormalParticipation.ASSISTED
    finally:
        state.close()


def test_boolean_cannot_grant_collaborative_environment_authority() -> None:
    policy = EnvironmentPolicy(frozenset())

    assert "human_approved" not in signature(policy.authorize).parameters


@pytest.mark.parametrize("approver", ("conductor", "research_conductor", "promotion_service"))
def test_trusted_services_cannot_approve_durable_environment_transitions(
    tmp_path: Path, approver: str
) -> None:
    state = StateService(
        StateServiceConfig(tmp_path, "environment-approval"),
        StateDependencies(human_approval=lambda _run, reviewer: reviewer == "human-reviewer"),
    )
    try:
        service = EnvironmentTransitionService(state, EnvironmentPolicy(frozenset()))
        proposal = service.propose("run-1", "research_conductor", _transition())

        with pytest.raises(EventValidationError, match="authenticated human"):
            service.approve("run-1", approver, proposal, _transition())
        with pytest.raises(EventValidationError, match="use approve_environment_transition"):
            state.append_event(
                AppendEventCommand("EnvironmentTransitionApproved", approver, "run-1", proposal, {})
            )
    finally:
        state.close()


def test_approval_cannot_be_reused_for_a_different_transition_payload(tmp_path: Path) -> None:
    run = RunPolicy(
        participation=ParticipationMode.COLLABORATIVE,
        formal_participation=FormalParticipation.ASSISTED,
        environment_frozen=False,
    )
    state = StateService(
        StateServiceConfig(tmp_path, "environment-binding"),
        StateDependencies(human_approval=lambda _run, reviewer: reviewer == "human-reviewer"),
    )
    try:
        service = EnvironmentTransitionService(state, EnvironmentPolicy(frozenset()))
        proposal = service.propose("run-1", "research_conductor", _transition())
        service.approve("run-1", "human-reviewer", proposal, _transition())
        mismatched = EnvironmentTransition("transition-1", _OLD, _OTHER, "new import policy")

        with pytest.raises(EnvironmentPolicyError, match="INVALID_HUMAN_APPROVAL"):
            service.authorize("run-1", run, mismatched)
    finally:
        state.close()
