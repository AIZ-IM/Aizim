from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import pytest

from aizim.domain import (
    Contribution,
    ContributionPayloadKind,
    EpochPair,
    FileLease,
    KnowledgeDelta,
    ParticipationMode,
    canonical_json,
    compute_base_epoch,
    compute_environment_fingerprint,
    sha256_bytes,
    sha256_file,
    sha256_json,
)


@dataclass(frozen=True, slots=True)
class CanonicalFixture:
    mode: ParticipationMode
    timestamp: datetime
    labels: set[str]
    path: Path


class ContributionFactory(Protocol):
    def __call__(self, *, payload_kind: str, file_version: int) -> Contribution: ...


@pytest.fixture
def epoch_pair() -> EpochPair:
    return EpochPair(base_epoch="a" * 64, knowledge_epoch=3)


@pytest.fixture
def contribution_factory(epoch_pair: EpochPair) -> ContributionFactory:
    def make_contribution(
        *,
        payload_kind: str,
        file_version: int,
    ) -> Contribution:
        kind = ContributionPayloadKind(payload_kind)
        snapshot_body = "theorem snapshot : True := by trivial"
        return Contribution(
            worker_id="proof-1",
            run_id="run-1",
            lease_id="lease-1",
            epoch_pair=epoch_pair,
            file_version=file_version,
            payload_kind=kind,
            environment_fingerprint="b" * 64,
            candidate_declaration="theorem candidate : True",
            candidate_proof="by trivial",
            patch_body="@@ -1 +1 @@\n-old\n+new" if kind is ContributionPayloadKind.PATCH else None,
            snapshot_body=snapshot_body if kind is ContributionPayloadKind.SNAPSHOT else None,
            payload_hash=(
                sha256_bytes(snapshot_body.encode())
                if kind is ContributionPayloadKind.SNAPSHOT
                else None
            ),
        )

    return make_contribution


def test_canonical_json_is_identical_for_differently_ordered_dictionaries() -> None:
    left = {"outer": {"z": 1, "a": 2}, "value": 3}
    right = {"value": 3, "outer": {"a": 2, "z": 1}}

    assert canonical_json(left) == canonical_json(right)
    assert sha256_json(left) == sha256_json(right)


def test_canonical_json_serializes_supported_domain_values() -> None:
    value = CanonicalFixture(
        mode=ParticipationMode.AUTONOMOUS,
        timestamp=datetime(2026, 7, 21, 10, 30, tzinfo=UTC),
        labels={"beta", "alpha"},
        path=Path("AizimSmoke/Research.lean"),
    )

    assert canonical_json(value) == (
        b'{"labels":["alpha","beta"],"mode":"autonomous",'
        b'"path":"AizimSmoke/Research.lean","timestamp":"2026-07-21T10:30:00Z"}'
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_non_finite_floats(value: float) -> None:
    with pytest.raises(ValueError):
        canonical_json(value)


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime(2026, 7, 21, 10, 30),
        datetime(2026, 7, 21, 10, 30, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_canonical_json_rejects_naive_and_non_utc_timestamps(timestamp: datetime) -> None:
    with pytest.raises(ValueError):
        canonical_json(timestamp)


def test_unknown_enum_values_are_rejected() -> None:
    with pytest.raises(ValueError):
        ParticipationMode("observer")


@pytest.mark.parametrize("base_epoch", ["a" * 63, "A" * 64, "g" * 64])
def test_epoch_pair_rejects_non_canonical_hashes(base_epoch: str) -> None:
    with pytest.raises(ValueError):
        EpochPair(base_epoch=base_epoch, knowledge_epoch=0)


def test_epoch_pair_rejects_negative_sequence() -> None:
    with pytest.raises(ValueError):
        EpochPair(base_epoch="a" * 64, knowledge_epoch=-1)


def test_patch_contribution_requires_body_and_expected_version(epoch_pair: EpochPair) -> None:
    with pytest.raises(ValueError):
        Contribution(
            worker_id="proof-1",
            run_id="run-1",
            lease_id="lease-1",
            epoch_pair=epoch_pair,
            file_version=None,
            payload_kind=ContributionPayloadKind.PATCH,
            environment_fingerprint="b" * 64,
            candidate_declaration="theorem candidate : True",
            candidate_proof="by trivial",
            patch_body=None,
        )


def test_snapshot_contribution_requires_matching_payload_hash(epoch_pair: EpochPair) -> None:
    with pytest.raises(ValueError):
        Contribution(
            worker_id="proof-1",
            run_id="run-1",
            lease_id="lease-1",
            epoch_pair=epoch_pair,
            file_version=2,
            payload_kind=ContributionPayloadKind.SNAPSHOT,
            environment_fingerprint="b" * 64,
            candidate_declaration="theorem candidate : True",
            candidate_proof="by trivial",
            snapshot_body="theorem candidate : True := by trivial",
            payload_hash="c" * 64,
        )


def test_file_version_staleness_depends_on_payload_kind(
    contribution_factory: ContributionFactory,
) -> None:
    patch = contribution_factory(payload_kind="patch", file_version=3)
    snapshot = contribution_factory(payload_kind="snapshot", file_version=3)
    current = patch.epoch_pair

    assert patch.staleness(current_epoch=current, current_file_version=4).file_version
    assert not snapshot.staleness(
        current_epoch=current,
        current_file_version=4,
    ).is_stale


@pytest.mark.parametrize("payload_kind", ["patch", "snapshot"])
def test_any_formal_epoch_mismatch_is_stale(
    contribution_factory: ContributionFactory,
    payload_kind: str,
) -> None:
    contribution = contribution_factory(payload_kind=payload_kind, file_version=3)
    current = EpochPair(base_epoch="c" * 64, knowledge_epoch=4)

    staleness = contribution.staleness(current_epoch=current, current_file_version=3)

    assert staleness.base_epoch
    assert staleness.knowledge_epoch
    assert staleness.is_stale


def test_file_lease_is_immutable_and_requires_utc_expiration(epoch_pair: EpochPair) -> None:
    lease = FileLease(
        lease_id="lease-1",
        worker_id="proof-1",
        run_id="run-1",
        document_id="doc-1",
        virtual_document_namespace="aizim://run-1/proof-1",
        epoch_pair=epoch_pair,
        file_version=0,
        content_hash="d" * 64,
        expires_at=datetime(2026, 7, 21, 11, tzinfo=UTC),
    )

    with pytest.raises(FrozenInstanceError):
        lease.__setattr__("file_version", 1)
    with pytest.raises(ValueError):
        FileLease(
            lease_id="lease-1",
            worker_id="proof-1",
            run_id="run-1",
            document_id="doc-1",
            virtual_document_namespace="aizim://run-1/proof-1",
            epoch_pair=epoch_pair,
            file_version=0,
            content_hash="d" * 64,
            expires_at=datetime(2026, 7, 21, 11),
        )


def test_knowledge_delta_advances_exactly_one_formal_sequence(epoch_pair: EpochPair) -> None:
    delta = KnowledgeDelta(
        previous_epoch=epoch_pair,
        new_epoch=EpochPair(base_epoch="e" * 64, knowledge_epoch=4),
        theorem_name="AizimSmoke.Research.candidate",
        complete_type="True",
        module="AizimSmoke.Research",
    )

    assert delta.new_epoch.knowledge_epoch == delta.previous_epoch.knowledge_epoch + 1


def test_environment_and_base_epoch_hashes_keep_boundaries_separate(tmp_path: Path) -> None:
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
    (tmp_path / "lakefile.toml").write_text('name = "AizimSmoke"\n')
    import_policy = {"Std": "toolchain", "AizimSmoke.Research": "promoted-root"}

    environment = compute_environment_fingerprint(
        tmp_path,
        import_policy,
        {"lean_runtime": "shared", "auto_implicit": False},
    )
    reordered = compute_environment_fingerprint(
        tmp_path,
        dict(reversed(tuple(import_policy.items()))),
        {"auto_implicit": False, "lean_runtime": "shared"},
    )
    base_before = compute_base_epoch(environment, {"AizimSmoke.Research": "1" * 64})
    base_after = compute_base_epoch(environment, {"AizimSmoke.Research": "2" * 64})

    assert environment == reordered
    assert base_before != base_after


def test_sha256_file_hashes_exact_bytes(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"aizim")

    assert sha256_file(artifact) == sha256_bytes(b"aizim")
