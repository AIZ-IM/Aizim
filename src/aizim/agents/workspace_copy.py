from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

_OPEN_DIRECTORY: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass(frozen=True, slots=True)
class ViewBuildError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class ViewSource:
    relative_path: PurePosixPath
    sha256: str


@dataclass(frozen=True, slots=True)
class ViewEntry:
    relative_path: str
    byte_length: int
    mode: int
    sha256: str


type CopyHook = Callable[[PurePosixPath], None]


def copy_source(
    root_fd: int,
    view_fd: int,
    source: ViewSource,
    after_copy: CopyHook | None,
) -> ViewEntry:
    source_fd = _open_source(root_fd, source.relative_path)
    try:
        metadata = os.fstat(source_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ViewBuildError("allowlisted source is not a private regular file")
        original = _snapshot(metadata)
        parent = _walk_parent(view_fd, tuple(source.relative_path.parts[:-1]), create=True)
        try:
            target_fd = os.open(
                source.relative_path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
        finally:
            os.close(parent)
        digest = hashlib.sha256()
        length = 0
        try:
            while chunk := os.read(source_fd, 1024 * 1024):
                digest.update(chunk)
                length += len(chunk)
                remaining = memoryview(chunk)
                while remaining:
                    written = os.write(target_fd, remaining)
                    remaining = remaining[written:]
            os.fchmod(target_fd, 0o444)
            os.fsync(target_fd)
        finally:
            os.close(target_fd)
        if digest.hexdigest() != source.sha256 or length != metadata.st_size:
            raise ViewBuildError("allowlisted source digest does not match")
    finally:
        os.close(source_fd)
    if after_copy is not None:
        after_copy(source.relative_path)
    _recheck_source(root_fd, source, original)
    return ViewEntry(source.relative_path.as_posix(), length, 0o444, source.sha256)


def _walk_parent(root_fd: int, parts: tuple[str, ...], create: bool) -> int:
    current = os.dup(root_fd)
    try:
        for part in parts:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=current)
            following = os.open(part, _OPEN_DIRECTORY, dir_fd=current)
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _open_source(root_fd: int, path: PurePosixPath) -> int:
    parent = _walk_parent(root_fd, tuple(path.parts[:-1]), create=False)
    try:
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)


def _snapshot(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


def _recheck_source(root_fd: int, source: ViewSource, original: tuple[int, int, int, int]) -> None:
    source_fd = _open_source(root_fd, source.relative_path)
    try:
        metadata = os.fstat(source_fd)
        digest = hashlib.sha256()
        while chunk := os.read(source_fd, 1024 * 1024):
            digest.update(chunk)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or _snapshot(metadata) != original
            or digest.hexdigest() != source.sha256
        ):
            raise ViewBuildError("allowlisted source changed during materialization")
    finally:
        os.close(source_fd)
