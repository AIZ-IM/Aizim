from __future__ import annotations

import os
import secrets
import stat
import unicodedata
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Final

_OPEN_DIRECTORY: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class DocumentIoError(RuntimeError):
    pass


def open_root(root: Path) -> int:
    try:
        descriptor = os.open(root, _OPEN_DIRECTORY)
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError
        return descriptor
    except OSError:
        raise DocumentIoError("DOCUMENT_PATH_DENIED") from None


def ensure_tree(root_fd: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_fd)
    root_device = os.fstat(root_fd).st_dev
    try:
        for part in parts:
            _validate_component(part)
            _reject_casefold_collision(current, part)
            with suppress(FileExistsError):
                os.mkdir(part, mode=0o700, dir_fd=current)
            following = os.open(part, _OPEN_DIRECTORY, dir_fd=current)
            _reject_casefold_collision(current, part)
            metadata = os.fstat(following)
            if metadata.st_dev != root_device:
                os.close(following)
                raise OSError
            os.close(current)
            current = following
        return current
    except OSError:
        os.close(current)
        raise DocumentIoError("DOCUMENT_PATH_DENIED") from None


def parent_fd(root_fd: int, relative: PurePosixPath, *, create: bool) -> int:
    parts = tuple(relative.parts[:-1])
    try:
        for part in (*parts, relative.name):
            _validate_component(part)
    except OSError:
        raise DocumentIoError("DOCUMENT_PATH_DENIED") from None
    if create:
        return ensure_tree(root_fd, parts)
    current = os.dup(root_fd)
    root_device = os.fstat(root_fd).st_dev
    try:
        for part in parts:
            _reject_casefold_collision(current, part)
            following = os.open(part, _OPEN_DIRECTORY, dir_fd=current)
            _reject_casefold_collision(current, part)
            metadata = os.fstat(following)
            if metadata.st_dev != root_device:
                os.close(following)
                raise OSError
            os.close(current)
            current = following
        return current
    except OSError:
        os.close(current)
        raise DocumentIoError("DOCUMENT_PATH_DENIED") from None


def read_relative(root_fd: int, relative: PurePosixPath) -> bytes:
    parent = parent_fd(root_fd, relative, create=False)
    try:
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError:
        os.close(parent)
        raise DocumentIoError("DOCUMENT_PATH_DENIED") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_dev != os.fstat(root_fd).st_dev
        ):
            raise DocumentIoError("DOCUMENT_PATH_DENIED")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)
        os.close(parent)


def mode_relative(root_fd: int, relative: PurePosixPath) -> int:
    parent = parent_fd(root_fd, relative, create=False)
    try:
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_dev != os.fstat(root_fd).st_dev
            ):
                raise OSError
            return stat.S_IMODE(metadata.st_mode)
        finally:
            os.close(descriptor)
    except OSError:
        raise DocumentIoError("DOCUMENT_PATH_DENIED") from None
    finally:
        os.close(parent)


def create_relative(
    root_fd: int, relative: PurePosixPath, body: bytes, *, mode: int = 0o600
) -> None:
    parent = parent_fd(root_fd, relative, create=True)
    descriptor: int | None = None
    created = False
    try:
        _reject_casefold_collision(parent, relative.name)
        descriptor = os.open(
            relative.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=parent,
        )
        created = True
        _reject_casefold_collision(parent, relative.name)
        _write(descriptor, body, mode, os.fstat(root_fd).st_dev)
        descriptor = None
        os.fsync(parent)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            with suppress(OSError):
                os.unlink(relative.name, dir_fd=parent)
        raise DocumentIoError("DOCUMENT_PATH_DENIED") from None
    finally:
        os.close(parent)


def create_content_addressed(root_fd: int, relative: PurePosixPath, body: bytes) -> None:
    parent = parent_fd(root_fd, relative, create=True)
    temporary = f".aizim-{secrets.token_hex(16)}.snapshot"
    descriptor: int | None = None
    try:
        _reject_casefold_collision(parent, relative.name)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        _write(descriptor, body, 0o600, os.fstat(root_fd).st_dev)
        descriptor = None
        with suppress(FileExistsError):
            os.link(
                temporary,
                relative.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
        os.unlink(temporary, dir_fd=parent)
        os.fsync(parent)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(OSError):
            os.unlink(temporary, dir_fd=parent)
        raise DocumentIoError("DOCUMENT_PATH_DENIED") from None
    finally:
        os.close(parent)


def unlink_relative(root_fd: int, relative: PurePosixPath) -> None:
    parent = parent_fd(root_fd, relative, create=False)
    try:
        _validate_leaf(root_fd, parent, relative.name)
        os.unlink(relative.name, dir_fd=parent)
        os.fsync(parent)
    except OSError:
        raise DocumentIoError("DOCUMENT_PATH_DENIED") from None
    finally:
        os.close(parent)


def replace_relative(root_fd: int, relative: PurePosixPath, body: bytes) -> None:
    parent = parent_fd(root_fd, relative, create=False)
    temporary = f".aizim-{secrets.token_hex(16)}.tmp"
    descriptor: int | None = None
    try:
        _validate_leaf(root_fd, parent, relative.name)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        _write(descriptor, body, 0o600, os.fstat(root_fd).st_dev)
        descriptor = None
        os.replace(temporary, relative.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
        _validate_leaf(root_fd, parent, relative.name)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(OSError):
            os.unlink(temporary, dir_fd=parent)
        raise DocumentIoError("DOCUMENT_PATH_DENIED") from None
    finally:
        os.close(parent)


def _write(descriptor: int, body: bytes, mode: int, root_device: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise OSError
    if metadata.st_dev != root_device:
        raise OSError
    os.fchmod(descriptor, mode)
    remaining = memoryview(body)
    while remaining:
        written = os.write(descriptor, remaining)
        remaining = remaining[written:]
    os.fsync(descriptor)
    os.close(descriptor)


def _validate_leaf(root_fd: int, parent: int, name: str) -> None:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_dev != os.fstat(root_fd).st_dev
        ):
            raise OSError
    finally:
        os.close(descriptor)


def _reject_casefold_collision(directory: int, name: str) -> None:
    folded = unicodedata.normalize("NFC", name).casefold()
    if any(
        entry != name and unicodedata.normalize("NFC", entry).casefold() == folded
        for entry in os.listdir(directory)
    ):
        raise OSError


def _validate_component(component: str) -> None:
    if (
        type(component) is not str
        or component in {"", ".", ".."}
        or os.sep in component
        or "\x00" in component
        or unicodedata.normalize("NFC", component) != component
    ):
        raise OSError
