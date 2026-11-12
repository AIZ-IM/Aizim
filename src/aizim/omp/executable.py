from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from aizim.domain import sha256_file
from aizim.runtime.executable import ResolvedExecutable

OMP_VERSION = "18.18.2"


class OmpExecutableError(RuntimeError):
    """An executable failure containing only a stable, non-secret reason code."""


def omp_version(path: Path) -> str:
    try:
        result = subprocess.run(
            (str(path), "--version"), capture_output=True, timeout=10,
            env={key: value for key, value in os.environ.items()
                 if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OmpExecutableError("OMP_VERSION_UNAVAILABLE") from error
    if result.returncode != 0 or not re.fullmatch(
        rb"omp/18\.18\.2(?:[ \t]+[^\r\n]*)?\r?\n?", result.stdout
    ):
        raise OmpExecutableError("UNSUPPORTED_OMP_VERSION")
    return f"omp/{OMP_VERSION}"


def resolve_omp(parent_environment: Mapping[str, str] | None = None) -> ResolvedExecutable:
    environment = os.environ if parent_environment is None else parent_environment
    selected = environment.get("AIZIM_OMP_EXECUTABLE") or shutil.which(
        "omp", path=environment.get("PATH", os.defpath)
    )
    if not selected:
        raise OmpExecutableError("OMP_EXECUTABLE_UNAVAILABLE")
    try:
        path = Path(selected).resolve(strict=True)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise OSError
        return ResolvedExecutable(path, omp_version(path), sha256_file(path))
    except OSError as error:
        raise OmpExecutableError("OMP_EXECUTABLE_UNAVAILABLE") from error


def revalidate_omp(executable: ResolvedExecutable) -> None:
    try:
        if (executable.path.resolve(strict=True) != executable.path
                or omp_version(executable.path) != executable.version
                or sha256_file(executable.path) != executable.sha256):
            raise OmpExecutableError("OMP_IMAGE_CHANGED")
    except OSError as error:
        raise OmpExecutableError("OMP_IMAGE_CHANGED") from error
