from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from aizim.domain import sha256_bytes
from aizim.lean.document_io import (
    DocumentIoError,
    create_content_addressed,
    ensure_tree,
    mode_relative,
    open_root,
    read_relative,
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


class ArtifactError(ValueError):
    pass


def _identifier(value: object, code: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ArtifactError(code)
    return value


def _hash(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ArtifactError("INVALID_ARTIFACT_REFERENCE")
    return value


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    run_id: str
    category: str
    content_hash: str
    relative_path: PurePosixPath
    media_type: str
    byte_length: int

    def __post_init__(self) -> None:
        run_id = _identifier(self.run_id, "INVALID_ARTIFACT_REFERENCE")
        category = _identifier(self.category, "INVALID_ARTIFACT_REFERENCE")
        digest = _hash(self.content_hash)
        expected = PurePosixPath(".aizim") / "artifacts" / run_id / category / digest
        if (
            self.relative_path != expected
            or type(self.media_type) is not str
            or not self.media_type
        ):
            raise ArtifactError("INVALID_ARTIFACT_REFERENCE")
        if type(self.byte_length) is not int or self.byte_length < 0:
            raise ArtifactError("INVALID_ARTIFACT_REFERENCE")


class ArtifactStore:
    def __init__(self, project_root: Path) -> None:
        if not isinstance(project_root, Path):
            raise ArtifactError("INVALID_ARTIFACT_ROOT")
        try:
            self._project_root = project_root.resolve(strict=True)
        except OSError:
            raise ArtifactError("INVALID_ARTIFACT_ROOT") from None

    def store(
        self, run_id: str, category: str, content: bytes, media_type: str
    ) -> ArtifactReference:
        _identifier(run_id, "INVALID_ARTIFACT_REQUEST")
        _identifier(category, "INVALID_ARTIFACT_REQUEST")
        if type(content) is not bytes or type(media_type) is not str or not media_type:
            raise ArtifactError("INVALID_ARTIFACT_REQUEST")
        digest = sha256_bytes(content)
        relative = PurePosixPath(".aizim") / "artifacts" / run_id / category / digest
        project = open_root(self._project_root)
        try:
            directory = ensure_tree(project, (".aizim", "artifacts", run_id, category))
            try:
                try:
                    existing = read_relative(directory, PurePosixPath(digest))
                except DocumentIoError:
                    create_content_addressed(directory, PurePosixPath(digest), content)
                    existing = read_relative(directory, PurePosixPath(digest))
                if existing != content or mode_relative(directory, PurePosixPath(digest)) != 0o600:
                    raise ArtifactError("ARTIFACT_INTEGRITY_MISMATCH")
            finally:
                os.close(directory)
        finally:
            os.close(project)
        return ArtifactReference(run_id, category, digest, relative, media_type, len(content))

    def read(self, reference: ArtifactReference) -> bytes:
        if type(reference) is not ArtifactReference:
            raise ArtifactError("INVALID_ARTIFACT_REFERENCE")
        project = open_root(self._project_root)
        try:
            body = read_relative(project, reference.relative_path)
            private = mode_relative(project, reference.relative_path) == 0o600
        finally:
            os.close(project)
        if (
            not private
            or len(body) != reference.byte_length
            or sha256_bytes(body) != reference.content_hash
        ):
            raise ArtifactError("ARTIFACT_INTEGRITY_MISMATCH")
        return body

    def load(self, run_id: str, category: str, content_hash: str) -> bytes:
        run_id = _identifier(run_id, "INVALID_ARTIFACT_REFERENCE")
        category = _identifier(category, "INVALID_ARTIFACT_REFERENCE")
        digest = _hash(content_hash)
        relative = PurePosixPath(".aizim") / "artifacts" / run_id / category / digest
        project = open_root(self._project_root)
        try:
            body = read_relative(project, relative)
            private = mode_relative(project, relative) == 0o600
        finally:
            os.close(project)
        if not private or sha256_bytes(body) != digest:
            raise ArtifactError("ARTIFACT_INTEGRITY_MISMATCH")
        return body
