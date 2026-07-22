from __future__ import annotations

import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .document_io import DocumentIoError, parent_fd


@dataclass(slots=True)
class LeanPathError(RuntimeError):
    code: str

    def __str__(self) -> str:
        return self.code


def validate_relative_path(relative: PurePosixPath) -> PurePosixPath:
    if not isinstance(relative, PurePosixPath) or relative.is_absolute():
        raise LeanPathError("DOCUMENT_PATH_DENIED")
    if not relative.parts or relative.name in {"", ".", ".."}:
        raise LeanPathError("DOCUMENT_PATH_DENIED")
    for part in relative.parts:
        if part in {"", ".", ".."} or "\x00" in part:
            raise LeanPathError("DOCUMENT_PATH_DENIED")
        if unicodedata.normalize("NFC", part) != part:
            raise LeanPathError("DOCUMENT_PATH_DENIED")
    return relative


class LeanPathPolicy:
    def __init__(self, root: Path, allowed: tuple[PurePosixPath, ...]) -> None:
        try:
            canonical = root.resolve(strict=True)
            descriptor = os.open(canonical, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise OSError
        except OSError:
            raise LeanPathError("DOCUMENT_PATH_DENIED") from None
        self._initialize(canonical, descriptor, allowed)

    @classmethod
    def from_descriptor(
        cls, descriptor: int, root: Path, allowed: tuple[PurePosixPath, ...]
    ) -> LeanPathPolicy:
        owned = -1
        try:
            owned = os.dup(descriptor)
            if not stat.S_ISDIR(os.fstat(owned).st_mode):
                raise OSError
        except OSError:
            if owned >= 0:
                os.close(owned)
            raise LeanPathError("DOCUMENT_PATH_DENIED") from None
        policy = cls.__new__(cls)
        policy._initialize(root, owned, allowed)
        return policy

    def _initialize(self, root: Path, descriptor: int, allowed: tuple[PurePosixPath, ...]) -> None:
        self._root = root
        self._descriptor = descriptor
        self._device = os.fstat(descriptor).st_dev
        self._allowed: set[PurePosixPath] = set()
        self._casefolded: dict[str, PurePosixPath] = {}
        try:
            for relative in allowed:
                self.register(relative)
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> LeanPathPolicy:
        return self

    def __exit__(self, *_ignored: object) -> None:
        self.close()

    @property
    def root(self) -> Path:
        return self._root

    def close(self) -> None:
        descriptor = self._descriptor
        self._descriptor = -1
        if descriptor >= 0:
            os.close(descriptor)

    def register(self, relative: PurePosixPath) -> None:
        canonical = validate_relative_path(relative)
        folded = canonical.as_posix().casefold()
        collision = self._casefolded.get(folded)
        if collision is not None and collision != canonical:
            raise LeanPathError("DOCUMENT_PATH_COLLISION")
        self._casefolded[folded] = canonical
        self._allowed.add(canonical)

    def resolve(self, relative: PurePosixPath) -> Path:
        canonical = self._require_allowed(relative)
        parent = self.parent_descriptor(canonical, create=False)
        try:
            leaf = os.open(canonical.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                metadata = os.fstat(leaf)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_dev != self._device
                ):
                    raise OSError
            finally:
                os.close(leaf)
        except OSError:
            raise LeanPathError("DOCUMENT_PATH_DENIED") from None
        finally:
            os.close(parent)
        self._validate_root_path()
        return self._root.joinpath(*canonical.parts)

    def parent_descriptor(self, relative: PurePosixPath, *, create: bool) -> int:
        canonical = self._require_allowed(relative)
        try:
            return parent_fd(self._descriptor, canonical, create=create)
        except DocumentIoError:
            raise LeanPathError("DOCUMENT_PATH_DENIED") from None

    def duplicate_root(self) -> int:
        if self._descriptor < 0:
            raise LeanPathError("DOCUMENT_PATH_DENIED")
        return os.dup(self._descriptor)

    def _require_allowed(self, relative: PurePosixPath) -> PurePosixPath:
        canonical = validate_relative_path(relative)
        if canonical not in self._allowed:
            raise LeanPathError("DOCUMENT_PATH_DENIED")
        return canonical

    def _validate_root_path(self) -> None:
        try:
            current = os.stat(self._root, follow_symlinks=False)
            owned = os.fstat(self._descriptor)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
                owned.st_dev,
                owned.st_ino,
            ):
                raise OSError
        except OSError:
            raise LeanPathError("DOCUMENT_PATH_DENIED") from None
