from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from tempfile import gettempdir

import pytest

from aizim.agents.workspace_view import (
    ViewBuildError,
    ViewSource,
    WorkspaceViewBuilder,
)


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def source(path: str, body: bytes) -> ViewSource:
    return ViewSource(PurePosixPath(path), digest(body))


def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir(parents=True)
    (root / "Nested").mkdir()
    (root / ".git").mkdir()
    (root / ".aizim").mkdir()
    (root / "Allowed.lean").write_bytes(b"theorem allowed : True := trivial\n")
    (root / "Unleased.lean").write_bytes(b"theorem private : True := trivial\n")
    (root / "Nested" / "Prereq.lean").write_bytes(b"theorem prereq : True := trivial\n")
    (root / "lakefile.toml").write_text('name = "fixture"\n')
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.1\n")
    (root / ".env").write_text("TOKEN=private\n")
    (root / "credentials.json").write_text('{"token":"private"}\n')
    (root / ".git" / "config").write_text("private")
    (root / ".aizim" / "state.sqlite3").write_text("private")
    return root


def test_materialized_view_contains_only_fresh_private_copies(tmp_path: Path) -> None:
    root = project(tmp_path)
    allowed = (root / "Allowed.lean").read_bytes()
    prerequisite = (root / "Nested" / "Prereq.lean").read_bytes()
    lakefile = (root / "lakefile.toml").read_bytes()
    toolchain = (root / "lean-toolchain").read_bytes()

    view = WorkspaceViewBuilder().materialize(
        root,
        (
            source("Allowed.lean", allowed),
            source("Nested/Prereq.lean", prerequisite),
            source("lakefile.toml", lakefile),
            source("lean-toolchain", toolchain),
        ),
    )
    try:
        copied = view.view_root / "Allowed.lean"
        assert copied.read_bytes() == allowed
        assert copied.stat().st_ino != (root / "Allowed.lean").stat().st_ino
        assert not copied.is_symlink()
        assert view.view_root.parent == Path(gettempdir()).resolve()
        assert not (view.view_root / "Unleased.lean").exists()
        assert not (view.view_root / ".git").exists()
        assert not (view.view_root / ".aizim").exists()
        assert stat.S_IMODE(view.view_root.stat().st_mode) == 0o700
        assert stat.S_IMODE(copied.stat().st_mode) == 0o444
        assert stat.S_IMODE((view.view_root / "Nested").stat().st_mode) == 0o555
        assert stat.S_IMODE(view.scratch_root.stat().st_mode) == 0o700
        assert tuple(entry.relative_path for entry in view.entries) == (
            "Allowed.lean",
            "Nested/Prereq.lean",
            "lakefile.toml",
            "lean-toolchain",
        )
        assert view.entries[0].byte_length == len(allowed)
        assert view.entries[0].mode == 0o444
        assert view.entries[0].sha256 == digest(allowed)
        assert len(view.manifest_sha256) == 64
    finally:
        view.close()
        view.close()
    assert not view.view_root.exists()
    assert not view.scratch_root.exists()


@pytest.mark.parametrize(
    "relative",
    (
        PurePosixPath("/absolute.lean"),
        PurePosixPath("../escape.lean"),
        PurePosixPath(".git/config"),
        PurePosixPath(".GIT/config"),
        PurePosixPath(".aizim/state.sqlite3"),
        PurePosixPath(".env"),
        PurePosixPath("credentials.json"),
        PurePosixPath("lake-manifest.json"),
    ),
)
def test_view_rejects_lexically_forbidden_sources(tmp_path: Path, relative: PurePosixPath) -> None:
    root = project(tmp_path)

    with pytest.raises(ViewBuildError):
        WorkspaceViewBuilder().materialize(root, (ViewSource(relative, "a" * 64),))


@pytest.mark.parametrize(
    "relative",
    (
        PurePosixPath(".GIT/config"),
        PurePosixPath(".env"),
        PurePosixPath("credentials.json"),
    ),
)
def test_view_rejects_non_source_files_with_matching_digest(
    tmp_path: Path, relative: PurePosixPath
) -> None:
    root = project(tmp_path)
    body = (root / relative).read_bytes()

    with pytest.raises(ViewBuildError):
        WorkspaceViewBuilder().materialize(root, (source(relative.as_posix(), body),))


@pytest.mark.parametrize("kind", ("source_symlink", "hard_link", "directory_symlink"))
def test_view_rejects_linked_sources(tmp_path: Path, kind: str) -> None:
    root = project(tmp_path)
    allowed = root / "Allowed.lean"
    if kind == "source_symlink":
        path = root / "Linked.lean"
        path.symlink_to(allowed)
    elif kind == "hard_link":
        path = root / "Linked.lean"
        os.link(allowed, path)
    else:
        linked = root / "Linked"
        linked.symlink_to(root / "Nested", target_is_directory=True)
        path = linked / "Prereq.lean"

    with pytest.raises(ViewBuildError):
        WorkspaceViewBuilder().materialize(
            root, (source(path.relative_to(root).as_posix(), path.read_bytes()),)
        )


def test_view_aborts_when_source_is_replaced_after_copy(tmp_path: Path) -> None:
    root = project(tmp_path)
    path = root / "Allowed.lean"
    body = path.read_bytes()

    def replace(_relative: PurePosixPath) -> None:
        path.unlink()
        path.symlink_to(root / "Unleased.lean")

    temporary_root = Path(gettempdir()).resolve()
    before = set(temporary_root.glob("aizim-view-*"))
    with pytest.raises(ViewBuildError):
        WorkspaceViewBuilder(after_copy=replace).materialize(root, (source("Allowed.lean", body),))
    assert set(temporary_root.glob("aizim-view-*")) == before


@pytest.mark.parametrize(
    "sources",
    (
        (ViewSource(PurePosixPath("Allowed.lean"), "0" * 64),),
        (
            ViewSource(PurePosixPath("Allowed.lean"), "a" * 64),
            ViewSource(PurePosixPath("Allowed.lean"), "a" * 64),
        ),
        (
            ViewSource(PurePosixPath("Nested"), "a" * 64),
            ViewSource(PurePosixPath("Nested/Prereq.lean"), "a" * 64),
        ),
    ),
)
def test_view_rejects_digest_duplicate_and_output_collision(
    tmp_path: Path, sources: tuple[ViewSource, ...]
) -> None:
    root = project(tmp_path)

    with pytest.raises(ViewBuildError):
        WorkspaceViewBuilder().materialize(root, sources)
