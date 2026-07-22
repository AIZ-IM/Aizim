from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

import aizim.lean.broker_storage as broker_storage
from aizim.domain import EpochPair, sha256_bytes
from aizim.lean.broker_storage import DocumentStorage
from aizim.lean.document_io import DocumentIoError, ensure_tree, open_root
from aizim.lean.path_policy import LeanPathError, LeanPathPolicy
from aizim.lean.project import materialize_smoke_project
from aizim.state.documents import DocumentState

DOCUMENT = PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean")
SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"


def _regular(root: Path, relative: PurePosixPath, body: bytes = b"example") -> Path:
    target = root.joinpath(*relative.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


def test_policy_resolves_only_an_allowlisted_private_regular_file(tmp_path: Path) -> None:
    target = _regular(tmp_path, DOCUMENT)
    with LeanPathPolicy(tmp_path, (DOCUMENT,)) as policy:
        assert policy.resolve(DOCUMENT) == target
        with pytest.raises(LeanPathError, match="DOCUMENT_PATH_DENIED"):
            policy.resolve(PurePosixPath("AizimSmoke/Base.lean"))


@pytest.mark.parametrize(
    "untrusted",
    [
        PurePosixPath("/tmp/outside.lean"),
        PurePosixPath("../outside.lean"),
        PurePosixPath("AizimSmoke/Workers/run-1/bad\x00.lean"),
        PurePosixPath("AizimSmoke/Workers/run-1/../outside.lean"),
    ],
)
def test_policy_rejects_noncanonical_or_outside_paths(
    tmp_path: Path, untrusted: PurePosixPath
) -> None:
    _regular(tmp_path, DOCUMENT)
    with (
        LeanPathPolicy(tmp_path, (DOCUMENT,)) as policy,
        pytest.raises(LeanPathError, match="DOCUMENT_PATH_DENIED"),
    ):
        policy.resolve(untrusted)


def test_policy_rejects_symlink_leaf_and_parent(tmp_path: Path) -> None:
    outside = _regular(tmp_path, PurePosixPath("outside.lean"))
    leaf = tmp_path.joinpath(*DOCUMENT.parts)
    leaf.parent.mkdir(parents=True)
    leaf.symlink_to(outside)
    with (
        LeanPathPolicy(tmp_path, (DOCUMENT,)) as policy,
        pytest.raises(LeanPathError, match="DOCUMENT_PATH_DENIED"),
    ):
        policy.resolve(DOCUMENT)

    leaf.unlink()
    leaf.parent.rmdir()
    (tmp_path / "AizimSmoke" / "Workers").rmdir()
    (tmp_path / "AizimSmoke").rmdir()
    (tmp_path / "AizimSmoke").symlink_to(tmp_path, target_is_directory=True)
    with (
        LeanPathPolicy(tmp_path, (DOCUMENT,)) as policy,
        pytest.raises(LeanPathError, match="DOCUMENT_PATH_DENIED"),
    ):
        policy.resolve(DOCUMENT)


def test_policy_rejects_hard_link_and_casefold_collision(tmp_path: Path) -> None:
    source = _regular(tmp_path, PurePosixPath("source.lean"))
    target = tmp_path.joinpath(*DOCUMENT.parts)
    target.parent.mkdir(parents=True)
    os.link(source, target)
    with (
        LeanPathPolicy(tmp_path, (DOCUMENT,)) as policy,
        pytest.raises(LeanPathError, match="DOCUMENT_PATH_DENIED"),
    ):
        policy.resolve(DOCUMENT)

    collision = PurePosixPath("aizimsmoke/workers/RUN-1/WORKER-1.lean")
    with pytest.raises(LeanPathError, match="DOCUMENT_PATH_COLLISION"):
        LeanPathPolicy(tmp_path, (DOCUMENT, collision))


@pytest.mark.parametrize(
    "untrusted",
    [
        PurePosixPath("/tmp/outside.lean"),
        PurePosixPath("../outside.lean"),
        PurePosixPath("AizimSmoke/Workers/run-1/bad\x00.lean"),
        PurePosixPath("AizimSmoke/Workers/run-1/other.lean"),
    ],
)
def test_storage_rejects_untrusted_worker_path(tmp_path: Path, untrusted: PurePosixPath) -> None:
    storage = DocumentStorage(tmp_path, SMOKE_ROOT)
    with pytest.raises(LeanPathError, match="DOCUMENT_PATH_DENIED"):
        storage.canonical_document("run-1", "worker-1", untrusted)


@pytest.mark.parametrize("attack", ["symlink_leaf", "symlink_parent", "hard_link"])
def test_storage_rejects_existing_link_attacks(tmp_path: Path, attack: str) -> None:
    root = materialize_smoke_project(tmp_path, "run-1", SMOKE_ROOT)
    target = root.joinpath(*DOCUMENT.parts)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "victim.lean"
    outside_file.write_bytes(b"outside\n")
    if attack == "symlink_parent":
        target.parent.parent.mkdir(parents=True)
        target.parent.symlink_to(outside, target_is_directory=True)
    else:
        target.parent.mkdir(parents=True)
        if attack == "symlink_leaf":
            target.symlink_to(outside_file)
        else:
            os.link(outside_file, target)

    storage = DocumentStorage(tmp_path, SMOKE_ROOT)
    with pytest.raises((DocumentIoError, LeanPathError)):
        storage.create("run-1", DOCUMENT, b"broker\n")
    assert outside_file.read_bytes() == b"outside\n"


def test_storage_rejects_casefold_collision_and_creates_private_file(
    tmp_path: Path,
) -> None:
    storage = DocumentStorage(tmp_path, SMOKE_ROOT)
    first = storage.canonical_document("run-1", "worker-1", DOCUMENT)
    storage.create("run-1", first, b"first\n")
    physical = tmp_path / ".aizim/run/run-1/lean-project" / first
    assert physical.stat().st_mode & 0o777 == 0o600
    collision = PurePosixPath("AizimSmoke/Workers/run-1/WORKER-1.lean")
    with pytest.raises(LeanPathError, match="DOCUMENT_PATH_COLLISION"):
        storage.create("run-1", collision, b"second\n")


def test_storage_never_deletes_a_preexisting_regular_leaf(tmp_path: Path) -> None:
    root = materialize_smoke_project(tmp_path, "run-1", SMOKE_ROOT)
    target = root.joinpath(*DOCUMENT.parts)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"preexisting\n")

    storage = DocumentStorage(tmp_path, SMOKE_ROOT)
    with pytest.raises(DocumentIoError, match="DOCUMENT_PATH_DENIED"):
        storage.create("run-1", DOCUMENT, b"broker\n")
    assert target.read_bytes() == b"preexisting\n"


def test_storage_pins_the_materialized_run_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = broker_storage.materialize_smoke_project
    outside = tmp_path / "outside"
    outside.mkdir()

    def replace_root(project_root: Path, run_id: str, smoke_root: Path) -> Path:
        root = original(project_root, run_id, smoke_root)
        root.rename(root.with_name("owned-lean-project"))
        root.symlink_to(outside, target_is_directory=True)
        return root

    monkeypatch.setattr(broker_storage, "materialize_smoke_project", replace_root)
    storage = DocumentStorage(tmp_path, SMOKE_ROOT)
    with pytest.raises(DocumentIoError, match="DOCUMENT_PATH_DENIED"):
        storage.create("run-1", DOCUMENT, b"broker\n")
    assert not (outside / DOCUMENT).exists()


def test_storage_rechecks_casefold_collisions_after_restart(tmp_path: Path) -> None:
    first = PurePosixPath("AizimSmoke/Workers/run-1/Worker-1.lean")
    collision = PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean")
    DocumentStorage(tmp_path, SMOKE_ROOT).create("run-1", first, b"first\n")

    restarted = DocumentStorage(tmp_path, SMOKE_ROOT)
    with pytest.raises(DocumentIoError, match="DOCUMENT_PATH_DENIED"):
        restarted.create("run-1", collision, b"second\n")


def test_storage_rejects_a_snapshot_with_nonprivate_mode(tmp_path: Path) -> None:
    storage = DocumentStorage(tmp_path, SMOKE_ROOT)
    body = b"snapshot\n"
    digest = sha256_bytes(body)
    with ThreadPoolExecutor(max_workers=8) as pool:
        tuple(pool.map(lambda _index: storage.store_snapshot("run-1", digest, body), range(8)))
    snapshot = tmp_path / ".aizim/artifacts/run-1/documents" / digest
    snapshot.chmod(0o644)

    with pytest.raises(DocumentIoError, match="DOCUMENT_SNAPSHOT_MISMATCH"):
        storage.store_snapshot("run-1", digest, body)


@pytest.mark.parametrize("kind", ["parent", "escape", "separator", "nul", "non_nfc"])
def test_descriptor_tree_rejects_untrusted_components(tmp_path: Path, kind: str) -> None:
    escaped = tmp_path.parent / f"outside-{tmp_path.name}"
    components = {
        "parent": "..",
        "escape": f"../{escaped.name}",
        "separator": "nested/name",
        "nul": "bad\x00",
        "non_nfc": "e\u0301",
    }
    (tmp_path / "nested").mkdir()
    descriptor = open_root(tmp_path)
    try:
        with pytest.raises(DocumentIoError, match="DOCUMENT_PATH_DENIED"):
            created = ensure_tree(descriptor, (components[kind],))
            os.close(created)
    finally:
        os.close(descriptor)
    assert not escaped.exists()


def test_storage_revalidates_projection_paths_before_recovery(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    (outside / "documents").mkdir(parents=True)
    (outside / "lean-project").mkdir()
    body = b"escaped replacement\n"
    digest = sha256_bytes(body)
    snapshot = outside / "documents" / digest
    snapshot.write_bytes(body)
    snapshot.chmod(0o600)
    victim = outside / "lean-project" / "victim.lean"
    victim.write_bytes(b"outside sentinel\n")
    victim.chmod(0o600)
    document = DocumentState(
        "doc",
        "../../../outside",
        "worker",
        "lease",
        PurePosixPath("victim.lean"),
        "AizimSmoke.Workers.W_bad",
        EpochPair("a" * 64, 0),
        0,
        digest,
        datetime(2026, 7, 23, tzinfo=UTC),
        "preparation",
    )

    with pytest.raises((DocumentIoError, LeanPathError), match="DOCUMENT_PATH_DENIED"):
        DocumentStorage(project, SMOKE_ROOT).restore(document)
    assert victim.read_bytes() == b"outside sentinel\n"
