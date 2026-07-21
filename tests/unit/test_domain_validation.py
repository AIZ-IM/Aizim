from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, assert_never

import pytest

from aizim.domain import (
    Contribution,
    ContributionPayloadKind,
    EpochPair,
    KnowledgeDelta,
    sha256_bytes,
)


@dataclass(frozen=True, slots=True)
class InvalidEpochPair:
    base_epoch: str
    knowledge_epoch: int


def _contribution(payload_kind: ContributionPayloadKind) -> Contribution:
    snapshot_body = "theorem snapshot : True := by trivial"
    match payload_kind:
        case ContributionPayloadKind.PATCH:
            patch_body = "@@ -1 +1 @@\n-old\n+new"
            active_snapshot = None
            payload_hash = None
        case ContributionPayloadKind.SNAPSHOT:
            patch_body = None
            active_snapshot = snapshot_body
            payload_hash = sha256_bytes(snapshot_body.encode())
        case unreachable:
            assert_never(unreachable)
    return Contribution(
        worker_id="proof-1",
        run_id="run-1",
        lease_id="lease-1",
        epoch_pair=EpochPair(base_epoch="a" * 64, knowledge_epoch=3),
        file_version=3,
        payload_kind=payload_kind,
        environment_fingerprint="b" * 64,
        candidate_declaration="theorem candidate : True",
        candidate_proof="by trivial",
        patch_body=patch_body,
        snapshot_body=active_snapshot,
        payload_hash=payload_hash,
    )


def _declaration_delta() -> KnowledgeDelta:
    return KnowledgeDelta(
        previous_epoch=EpochPair(base_epoch="a" * 64, knowledge_epoch=3),
        new_epoch=EpochPair(base_epoch="e" * 64, knowledge_epoch=4),
        theorem_name="AizimSmoke.Research.candidate",
        complete_type="True",
        module="AizimSmoke.Research",
    )


@pytest.mark.parametrize("body", [True, 1, ""])
def test_patch_contribution_rejects_non_string_or_empty_body(body: str | int | bool) -> None:
    # Given
    contribution = _contribution(ContributionPayloadKind.PATCH)

    # When / Then
    with pytest.raises(ValueError):
        replace(contribution, patch_body=body)


@pytest.mark.parametrize("body", [True, 1, ""])
def test_snapshot_contribution_rejects_non_string_or_empty_body(body: str | int | bool) -> None:
    # Given
    contribution = _contribution(ContributionPayloadKind.SNAPSHOT)

    # When / Then
    with pytest.raises(ValueError):
        replace(contribution, snapshot_body=body)


@pytest.mark.parametrize(
    ("field_name", "invalid_epoch"),
    [
        ("previous_epoch", InvalidEpochPair(base_epoch="a" * 64, knowledge_epoch=3)),
        ("new_epoch", InvalidEpochPair(base_epoch="e" * 64, knowledge_epoch=4)),
    ],
)
def test_knowledge_delta_rejects_noncanonical_nested_epoch_record(
    field_name: Literal["previous_epoch", "new_epoch"],
    invalid_epoch: InvalidEpochPair,
) -> None:
    # Given
    delta = _declaration_delta()

    # When / Then
    with pytest.raises(ValueError):
        replace(delta, **{field_name: invalid_epoch})


def test_environment_delta_requires_changed_fingerprint() -> None:
    # Given
    fingerprint = "f" * 64

    # When / Then
    with pytest.raises(ValueError):
        KnowledgeDelta(
            previous_epoch=EpochPair(base_epoch="a" * 64, knowledge_epoch=3),
            new_epoch=EpochPair(base_epoch="e" * 64, knowledge_epoch=4),
            old_environment_fingerprint=fingerprint,
            new_environment_fingerprint=fingerprint,
        )


@pytest.mark.parametrize("payload_kind", list(ContributionPayloadKind))
def test_base_epoch_only_mismatch_is_stale(payload_kind: ContributionPayloadKind) -> None:
    # Given
    contribution = _contribution(payload_kind)
    current = EpochPair(base_epoch="c" * 64, knowledge_epoch=3)

    # When
    staleness = contribution.staleness(current_epoch=current, current_file_version=3)

    # Then
    assert staleness.base_epoch
    assert not staleness.knowledge_epoch
    assert staleness.is_stale


@pytest.mark.parametrize("payload_kind", list(ContributionPayloadKind))
def test_knowledge_epoch_only_mismatch_is_stale(payload_kind: ContributionPayloadKind) -> None:
    # Given
    contribution = _contribution(payload_kind)
    current = EpochPair(base_epoch="a" * 64, knowledge_epoch=4)

    # When
    staleness = contribution.staleness(current_epoch=current, current_file_version=3)

    # Then
    assert not staleness.base_epoch
    assert staleness.knowledge_epoch
    assert staleness.is_stale
