from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import Final

from .workspace_copy import CopyHook, ViewBuildError, ViewEntry, ViewSource, copy_source

_RESERVED_PARTS: Final = frozenset({".git", ".aizim"})
_RESERVED_FILES: Final = frozenset({"lake-manifest.json"})
_CONTROL_FILES: Final = frozenset({"lakefile.toml", "lean-toolchain"})
_OPEN_DIRECTORY: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class WorkspaceView:
    __slots__ = ("_closed", "entries", "manifest_sha256", "scratch_root", "view_root")

    def __init__(
        self,
        view_root: Path,
        scratch_root: Path,
        entries: tuple[ViewEntry, ...],
        manifest_sha256: str,
    ) -> None:
        self.view_root = view_root
        self.scratch_root = scratch_root
        self.entries = entries
        self.manifest_sha256 = manifest_sha256
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        try:
            _remove_private_tree(self.view_root)
        finally:
            _remove_private_tree(self.scratch_root)
        self._closed = True


class WorkspaceViewBuilder:
    def __init__(self, after_copy: CopyHook | None = None) -> None:
        self._after_copy = after_copy

    def materialize(self, project_root: Path, sources: Iterable[ViewSource]) -> WorkspaceView:
        ordered = _validate_sources(tuple(sources))
        root = project_root.resolve(strict=True)
        if not root.is_dir():
            raise ViewBuildError("canonical project root is not a directory")
        temporary_root = _private_temporary_root()
        if root == temporary_root or root in temporary_root.parents:
            raise ViewBuildError("workspace view root must be outside the canonical project")
        view_root, scratch_root = _create_ephemeral_roots(temporary_root)
        root_fd = -1
        view_fd = -1
        try:
            root_fd = os.open(root, _OPEN_DIRECTORY)
            view_fd = os.open(view_root, _OPEN_DIRECTORY)
            entries = tuple(
                copy_source(root_fd, view_fd, source, self._after_copy) for source in ordered
            )
            _seal_view(view_root, view_fd)
            manifest = json.dumps(
                [
                    {
                        "byte_length": entry.byte_length,
                        "mode": entry.mode,
                        "relative_path": entry.relative_path,
                        "sha256": entry.sha256,
                    }
                    for entry in entries
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            return WorkspaceView(
                view_root,
                scratch_root,
                entries,
                hashlib.sha256(manifest).hexdigest(),
            )
        except ViewBuildError:
            _remove_private_tree(view_root)
            _remove_private_tree(scratch_root)
            raise
        except (OSError, ValueError) as error:
            _remove_private_tree(view_root)
            _remove_private_tree(scratch_root)
            raise ViewBuildError("workspace view construction failed") from error
        finally:
            if view_fd >= 0:
                os.close(view_fd)
            if root_fd >= 0:
                os.close(root_fd)


def _validate_sources(sources: tuple[ViewSource, ...]) -> tuple[ViewSource, ...]:
    paths: list[tuple[str, ...]] = []
    validated: list[ViewSource] = []
    for source in sources:
        relative = source.relative_path
        parts = relative.parts
        folded_parts = tuple(part.casefold() for part in parts)
        valid_digest = len(source.sha256) == 64 and all(
            character in "0123456789abcdef" for character in source.sha256
        )
        if (
            relative.is_absolute()
            or not parts
            or any(part in {"", ".", ".."} for part in parts)
            or any(part.startswith(".") for part in parts)
            or any(part in _RESERVED_PARTS for part in folded_parts)
            or relative.name.casefold() in _RESERVED_FILES
            or (relative.suffix != ".lean" and relative.name not in _CONTROL_FILES)
            or not valid_digest
        ):
            raise ViewBuildError("source allowlist contains an invalid entry")
        path_parts = tuple(parts)
        if path_parts in paths or any(
            _is_prefix(path_parts, existing) or _is_prefix(existing, path_parts)
            for existing in paths
        ):
            raise ViewBuildError("source allowlist contains an output collision")
        paths.append(path_parts)
        validated.append(source)
    return tuple(sorted(validated, key=lambda item: item.relative_path.as_posix()))


def _is_prefix(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return len(left) < len(right) and right[: len(left)] == left


def _private_temporary_root() -> Path:
    root = Path(tempfile.gettempdir()).resolve(strict=True)
    metadata = root.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ViewBuildError("temporary root is not private to the current user")
    return root


def _create_ephemeral_roots(temporary_root: Path) -> tuple[Path, Path]:
    view_root: Path | None = None
    scratch_root: Path | None = None
    try:
        view_root = Path(tempfile.mkdtemp(prefix="aizim-view-", dir=temporary_root))
        scratch_root = Path(tempfile.mkdtemp(prefix="aizim-scratch-", dir=temporary_root))
        os.chmod(view_root, 0o700)
        os.chmod(scratch_root, 0o700)
    except OSError as error:
        if view_root is not None:
            _remove_private_tree(view_root)
        if scratch_root is not None:
            _remove_private_tree(scratch_root)
        raise ViewBuildError("workspace view roots could not be created") from error
    return view_root, scratch_root


def _seal_view(root: Path, root_fd: int) -> None:
    for directory, child_directories, _files in os.walk(root, topdown=False):
        for child in child_directories:
            os.chmod(Path(directory) / child, 0o555, follow_symlinks=False)
        directory_fd = os.open(directory, _OPEN_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    os.fsync(root_fd)
    os.chmod(root, 0o700)


def _remove_private_tree(root: Path) -> None:
    try:
        metadata = os.lstat(root)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode):
        root.unlink(missing_ok=True)
        return
    for directory, child_directories, _files in os.walk(root, topdown=False):
        for child in child_directories:
            child_path = Path(directory) / child
            with suppress(FileNotFoundError):
                if stat.S_ISDIR(os.lstat(child_path).st_mode):
                    os.chmod(child_path, 0o700, follow_symlinks=False)
        os.chmod(directory, 0o700, follow_symlinks=False)
    shutil.rmtree(root)
