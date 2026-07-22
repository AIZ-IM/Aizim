from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath

from aizim.domain import sha256_bytes

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


class FoundationArtifactError(RuntimeError): ...


def read_registered_artifact(
    project: Path,
    run_id: str,
    name: str,
    relative: str,
    byte_length: int,
    digest: str,
) -> bytes:
    expected = (".aizim", "artifacts", run_id, name)
    if PurePosixPath(relative).parts != expected:
        raise FoundationArtifactError("ARTIFACT_PATH_INVALID")
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(project, _DIRECTORY_FLAGS))
        for component in expected[:-1]:
            descriptors.append(os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptors[-1]))
        descriptors.append(os.open(name, _FILE_FLAGS, dir_fd=descriptors[-1]))
        metadata = os.fstat(descriptors[-1])
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != byte_length
        ):
            raise FoundationArtifactError("ARTIFACT_PATH_INVALID")
        body = _read_exact(descriptors[-1], byte_length)
        final = os.fstat(descriptors[-1])
        stable = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        ) == (
            final.st_dev,
            final.st_ino,
            final.st_mode,
            final.st_nlink,
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        )
        if not stable or sha256_bytes(body) != digest:
            raise FoundationArtifactError("ARTIFACT_HASH_MISMATCH")
        return body
    except OSError as error:
        raise FoundationArtifactError("ARTIFACT_PATH_INVALID") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_exact(descriptor: int, byte_length: int) -> bytes:
    body = bytearray()
    while len(body) <= byte_length:
        chunk = os.read(descriptor, min(65_536, byte_length + 1 - len(body)))
        if not chunk:
            break
        body.extend(chunk)
    if len(body) != byte_length:
        raise FoundationArtifactError("ARTIFACT_HASH_MISMATCH")
    return bytes(body)
