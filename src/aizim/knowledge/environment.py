from __future__ import annotations

import re
from dataclasses import dataclass
from typing import assert_never

from aizim.domain import FormalParticipation, ParticipationMode, RunPolicy
from aizim.domain.serialization import JsonValue
from aizim.state import AppendEventCommand, StateService


class EnvironmentPolicyError(ValueError):
    pass


def _hash(value: object, code: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise EnvironmentPolicyError(code)
    return value


@dataclass(frozen=True, slots=True)
class EnvironmentTransition:
    transition_id: str
    old_fingerprint: str
    new_fingerprint: str
    reason: str

    def __post_init__(self) -> None:
        if type(self.transition_id) is not str or not self.transition_id:
            raise EnvironmentPolicyError("INVALID_TRANSITION")
        _hash(self.old_fingerprint, "INVALID_TRANSITION")
        _hash(self.new_fingerprint, "INVALID_TRANSITION")
        if self.old_fingerprint == self.new_fingerprint:
            raise EnvironmentPolicyError("INVALID_TRANSITION")
        if type(self.reason) is not str or not self.reason:
            raise EnvironmentPolicyError("INVALID_TRANSITION")


@dataclass(frozen=True, slots=True)
class EnvironmentDecision:
    approved: bool
    formal_participation: FormalParticipation


class EnvironmentPolicy:
    def __init__(self, predeclared_fingerprints: frozenset[str]) -> None:
        if type(predeclared_fingerprints) is not frozenset:
            raise EnvironmentPolicyError("INVALID_ENVIRONMENT_POLICY")
        for fingerprint in predeclared_fingerprints:
            _hash(fingerprint, "INVALID_ENVIRONMENT_POLICY")
        self._predeclared = predeclared_fingerprints

    def authorize(self, run: RunPolicy, transition: EnvironmentTransition) -> EnvironmentDecision:
        if type(run) is not RunPolicy or type(transition) is not EnvironmentTransition:
            raise EnvironmentPolicyError("INVALID_TRANSITION")
        if run.environment_frozen:
            raise EnvironmentPolicyError("ENVIRONMENT_FROZEN")
        match run.participation:
            case ParticipationMode.AUTONOMOUS:
                if transition.new_fingerprint not in self._predeclared:
                    raise EnvironmentPolicyError("TRANSITION_NOT_PREDECLARED")
                return EnvironmentDecision(True, FormalParticipation.ASSISTED)
            case ParticipationMode.COLLABORATIVE:
                raise EnvironmentPolicyError("HUMAN_APPROVAL_REQUIRED")
            case ParticipationMode.LEARNING:
                raise EnvironmentPolicyError("TRANSITION_NOT_ALLOWED")
            case unreachable:
                assert_never(unreachable)

    def _authorize_human_approved(
        self, run: RunPolicy, transition: EnvironmentTransition
    ) -> EnvironmentDecision:
        if type(run) is not RunPolicy or type(transition) is not EnvironmentTransition:
            raise EnvironmentPolicyError("INVALID_TRANSITION")
        if run.environment_frozen:
            raise EnvironmentPolicyError("ENVIRONMENT_FROZEN")
        match run.participation:
            case ParticipationMode.COLLABORATIVE:
                return EnvironmentDecision(True, FormalParticipation.ASSISTED)
            case ParticipationMode.AUTONOMOUS:
                raise EnvironmentPolicyError("TRANSITION_NOT_ALLOWED")
            case ParticipationMode.LEARNING:
                raise EnvironmentPolicyError("TRANSITION_NOT_ALLOWED")
            case unreachable:
                assert_never(unreachable)


class EnvironmentTransitionService:
    def __init__(self, state: StateService, policy: EnvironmentPolicy) -> None:
        if type(state) is not StateService or type(policy) is not EnvironmentPolicy:
            raise EnvironmentPolicyError("INVALID_ENVIRONMENT_POLICY")
        self._state, self._policy = state, policy

    def authorize(
        self, run_id: str, run: RunPolicy, transition: EnvironmentTransition
    ) -> EnvironmentDecision:
        if type(run_id) is not str or not run_id:
            raise EnvironmentPolicyError("INVALID_TRANSITION")
        match run.participation:
            case ParticipationMode.COLLABORATIVE:
                if self._human_approved(run_id, transition):
                    return self._policy._authorize_human_approved(run, transition)
                return self._policy.authorize(run, transition)
            case ParticipationMode.AUTONOMOUS | ParticipationMode.LEARNING:
                return self._policy.authorize(run, transition)
            case unreachable:
                assert_never(unreachable)

    def propose(self, run_id: str, proposer: str, transition: EnvironmentTransition) -> str:
        if type(run_id) is not str or not run_id or type(proposer) is not str or not proposer:
            raise EnvironmentPolicyError("INVALID_TRANSITION")
        event = self._state.append_event(
            AppendEventCommand(
                "EnvironmentTransitionProposed",
                proposer,
                run_id,
                None,
                _transition_payload(transition),
            )
        )
        return event.envelope.event_id

    def approve(
        self, run_id: str, reviewer: str, proposal_event_id: str, transition: EnvironmentTransition
    ) -> str:
        event = self._state.approve_environment_transition(
            run_id,
            reviewer,
            proposal_event_id,
            transition.transition_id,
            transition.old_fingerprint,
            transition.new_fingerprint,
            transition.reason,
        )
        return event.envelope.event_id

    def _human_approved(self, run_id: str, transition: EnvironmentTransition) -> bool:
        events = tuple(record.envelope for record in self._state.query_events(run_id))
        payload = _transition_payload(transition)
        proposals = tuple(
            event
            for event in events
            if event.event_type == "EnvironmentTransitionProposed"
            and event.payload.get("transition_id") == transition.transition_id
        )
        proposal = next(
            (event for event in reversed(proposals) if event.payload == payload),
            None,
        )
        if proposal is None:
            if proposals or any(
                event.event_type == "EnvironmentTransitionApproved"
                and event.payload.get("transition_id") == transition.transition_id
                for event in events
            ):
                raise EnvironmentPolicyError("INVALID_HUMAN_APPROVAL")
            return False
        expected = {**payload, "reviewer": None}
        for approval in reversed(events):
            if approval.event_type != "EnvironmentTransitionApproved":
                continue
            if approval.payload.get("transition_id") != transition.transition_id:
                continue
            reviewer = approval.payload.get("reviewer")
            if approval.causation_id != proposal.event_id or reviewer != approval.actor:
                raise EnvironmentPolicyError("INVALID_HUMAN_APPROVAL")
            if {**approval.payload, "reviewer": None} != expected:
                raise EnvironmentPolicyError("INVALID_HUMAN_APPROVAL")
            return True
        return False


def _transition_payload(transition: EnvironmentTransition) -> dict[str, JsonValue]:
    return {
        "transition_id": transition.transition_id,
        "old_fingerprint": transition.old_fingerprint,
        "new_fingerprint": transition.new_fingerprint,
        "reason": transition.reason,
    }
