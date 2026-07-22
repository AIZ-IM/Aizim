from __future__ import annotations

import pytest

from aizim.domain import sha256_bytes
from aizim.knowledge.contributions import (
    ByteEdit,
    ContributionValidationError,
    PatchPayload,
    SnapshotPayload,
    validate_candidate_source,
)

_SOURCE = b"import Std\n\ntheorem candidate : True := by\n  trivial\n"


def test_patch_payload_applies_ordered_utf8_byte_edits() -> None:
    start = _SOURCE.index(b"trivial")
    payload = PatchPayload(
        expected_file_version=3,
        expected_content_hash=sha256_bytes(_SOURCE),
        edits=(ByteEdit(start, start + len(b"trivial"), b"exact True.intro"),),
    )

    assert payload.apply(_SOURCE) == _SOURCE.replace(b"trivial", b"exact True.intro")


@pytest.mark.parametrize(
    "edits",
    (
        (ByteEdit(0, 2, b""), ByteEdit(1, 3, b"")),
        (ByteEdit(1, 1, b""),),
    ),
)
def test_patch_payload_rejects_overlaps_and_codepoint_splits(edits: tuple[ByteEdit, ...]) -> None:
    source = "é theorem candidate : True := by trivial\n".encode()
    payload = PatchPayload(0, sha256_bytes(source), edits)

    with pytest.raises(ContributionValidationError):
        payload.apply(source)


def test_snapshot_payload_requires_utf8_and_matching_hash() -> None:
    with pytest.raises(ContributionValidationError):
        SnapshotPayload(b"\xff", "a" * 64)

    with pytest.raises(ContributionValidationError):
        SnapshotPayload(_SOURCE, "b" * 64)

    snapshot = SnapshotPayload(_SOURCE, sha256_bytes(_SOURCE))

    assert snapshot.source == _SOURCE


def test_candidate_source_rejects_unsafe_tokens_and_unapproved_imports() -> None:
    with pytest.raises(ContributionValidationError):
        validate_candidate_source(
            b"import Mathlib\ntheorem candidate : True := by sorry\n",
            "candidate",
            "True",
            ("Std",),
        )

    with pytest.raises(ContributionValidationError):
        validate_candidate_source(
            b"import Std\ntheorem other : True := by trivial\n",
            "candidate",
            "True",
            ("Std",),
        )

    with pytest.raises(ContributionValidationError, match="EXTRA_TOP_LEVEL_DECLARATION"):
        validate_candidate_source(
            b"import Std\ntheorem candidate : True := True.intro\n"
            b"theorem untracked : True := True.intro\n",
            "candidate",
            "True",
            ("Std",),
        )


def test_candidate_source_ignores_comments_and_strings_when_scanning_tokens() -> None:
    source = (
        b'import Std\n-- sorry is not a proof\n-- #eval "admit"\n'
        b"theorem candidate : True := by trivial\n"
    )

    validate_candidate_source(source, "candidate", "True", ("Std",))


@pytest.mark.parametrize("command", ("#eval", "#check", "set_option", "attribute", "example"))
def test_candidate_source_rejects_extra_lean_commands(command: str) -> None:
    source = f"import Std\n{command} True\ntheorem candidate : True := True.intro\n".encode()

    with pytest.raises(ContributionValidationError, match="FORBIDDEN_LEAN_COMMAND"):
        validate_candidate_source(source, "candidate", "True", ("Std",))
