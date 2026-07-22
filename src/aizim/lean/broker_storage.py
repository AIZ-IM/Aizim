from __future__ import annotations

import fcntl
import os
import re
from pathlib import Path, PurePosixPath

from aizim.domain import sha256_bytes
from aizim.state.documents import DocumentState

from .document_io import (
    DocumentIoError,
    create_content_addressed,
    create_relative,
    ensure_tree,
    mode_relative,
    open_root,
    read_relative,
    replace_relative,
    unlink_relative,
)
from .path_policy import LeanPathError, LeanPathPolicy, validate_relative_path
from .project import materialize_smoke_project

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def _identifier(value: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise LeanPathError("DOCUMENT_PATH_DENIED")
    return value


class DocumentStorage:
    def __init__(self, project_root: Path, smoke_root: Path) -> None:
        self._project_root = project_root.resolve(strict=True)
        self._smoke_root = smoke_root.resolve(strict=True)
        self._allowed: dict[str, set[PurePosixPath]] = {}

    def canonical_document(
        self, run_id: str, worker_id: str, relative: PurePosixPath
    ) -> PurePosixPath:
        _identifier(run_id)
        _identifier(worker_id)
        canonical = validate_relative_path(relative)
        expected = PurePosixPath(f"AizimSmoke/Workers/{run_id}/{worker_id}.lean")
        if canonical != expected:
            raise LeanPathError("DOCUMENT_PATH_DENIED")
        return canonical

    def create(self, run_id: str, relative: PurePosixPath, body: bytes) -> str:
        allowed = self._allowed.setdefault(run_id, set())
        if relative in allowed:
            raise DocumentIoError("DOCUMENT_ALREADY_LEASED")
        self._reject_collision(allowed, relative)
        allowed.add(relative)
        created = False
        try:
            with self._policy(run_id, tuple(allowed)) as policy:
                descriptor = policy.duplicate_root()
                try:
                    create_relative(descriptor, relative, body)
                    created = True
                finally:
                    os.close(descriptor)
            content_hash = sha256_bytes(body)
            self.store_snapshot(run_id, content_hash, body)
            return content_hash
        except BaseException:
            if created:
                self.discard(run_id, relative)
            else:
                allowed.discard(relative)
            raise

    def read(self, document: DocumentState) -> bytes:
        self._validate_document(document)
        allowed = self._allowed.setdefault(document.run_id, set())
        self._reject_collision(allowed, document.relative_path)
        allowed.add(document.relative_path)
        with self._policy(document.run_id, tuple(allowed)) as policy:
            policy.resolve(document.relative_path)
            descriptor = policy.duplicate_root()
            try:
                return read_relative(descriptor, document.relative_path)
            finally:
                os.close(descriptor)

    def resolved_path(self, document: DocumentState) -> tuple[Path, Path]:
        self._validate_document(document)
        allowed = self._allowed.setdefault(document.run_id, set())
        self._reject_collision(allowed, document.relative_path)
        allowed.add(document.relative_path)
        with self._policy(document.run_id, tuple(allowed)) as policy:
            return policy.root, policy.resolve(document.relative_path)

    def replace(self, document: DocumentState, body: bytes) -> None:
        self._validate_document(document)
        allowed = self._allowed.setdefault(document.run_id, set())
        self._reject_collision(allowed, document.relative_path)
        allowed.add(document.relative_path)
        with self._policy(document.run_id, tuple(allowed)) as policy:
            descriptor = policy.duplicate_root()
            try:
                replace_relative(descriptor, document.relative_path, body)
            finally:
                os.close(descriptor)

    def register(self, document: DocumentState) -> None:
        self._validate_document(document)
        allowed = self._allowed.setdefault(document.run_id, set())
        self._reject_collision(allowed, document.relative_path)
        allowed.add(document.relative_path)

    def store_snapshot(self, run_id: str, content_hash: str, body: bytes) -> None:
        if sha256_bytes(body) != content_hash:
            raise DocumentIoError("DOCUMENT_SNAPSHOT_MISMATCH")
        descriptor = self._artifact_descriptor(run_id)
        relative = PurePosixPath(content_hash)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                existing = read_relative(descriptor, relative)
            except DocumentIoError:
                create_content_addressed(descriptor, relative, body)
                existing = read_relative(descriptor, relative)
            if (
                existing != body
                or sha256_bytes(existing) != content_hash
                or mode_relative(descriptor, relative) != 0o600
            ):
                raise DocumentIoError("DOCUMENT_SNAPSHOT_MISMATCH")
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def restore(self, document: DocumentState) -> None:
        self._validate_document(document)
        descriptor = self._artifact_descriptor(document.run_id)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            relative = PurePosixPath(document.content_hash)
            body = read_relative(descriptor, relative)
            private = mode_relative(descriptor, relative) == 0o600
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        if sha256_bytes(body) != document.content_hash or not private:
            raise DocumentIoError("DOCUMENT_SNAPSHOT_MISMATCH")
        self.replace(document, body)

    def discard(self, run_id: str, relative: PurePosixPath) -> None:
        try:
            with self._policy(run_id, (relative,)) as policy:
                descriptor = policy.duplicate_root()
                try:
                    unlink_relative(descriptor, relative)
                finally:
                    os.close(descriptor)
        except (DocumentIoError, LeanPathError):
            pass
        finally:
            self._allowed.setdefault(run_id, set()).discard(relative)

    def _run_project(self, run_id: str) -> Path:
        _identifier(run_id)
        return materialize_smoke_project(self._project_root, run_id, self._smoke_root)

    def _policy(self, run_id: str, allowed: tuple[PurePosixPath, ...]) -> LeanPathPolicy:
        root = self._run_project(run_id)
        project = open_root(self._project_root)
        try:
            descriptor = ensure_tree(project, (".aizim", "run", run_id, "lean-project"))
        finally:
            os.close(project)
        try:
            return LeanPathPolicy.from_descriptor(descriptor, root, allowed)
        finally:
            os.close(descriptor)

    def _artifact_descriptor(self, run_id: str) -> int:
        _identifier(run_id)
        project = open_root(self._project_root)
        try:
            return ensure_tree(project, (".aizim", "artifacts", run_id, "documents"))
        finally:
            os.close(project)

    @staticmethod
    def _reject_collision(allowed: set[PurePosixPath], relative: PurePosixPath) -> None:
        folded = relative.as_posix().casefold()
        if any(item.as_posix().casefold() == folded and item != relative for item in allowed):
            raise LeanPathError("DOCUMENT_PATH_COLLISION")

    def _validate_document(self, document: DocumentState) -> None:
        self.canonical_document(document.run_id, document.worker_id, document.relative_path)
