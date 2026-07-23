from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from aizim import __version__

DISTRIBUTION_ENVIRONMENT: Final[frozenset[str]] = frozenset(
    {
        "AIZIM_DISTRIBUTION_MODE",
        "AIZIM_DISTRIBUTION_VERSION",
        "AIZIM_DISTRIBUTION_TARGET",
        "AIZIM_CODEX_EXECUTABLE",
        "AIZIM_DISTRIBUTION_MANIFEST_SHA256",
        "AIZIM_PLATFORM_MANIFEST_SHA256",
    }
)
_TARGETS: Final[frozenset[str]] = frozenset(
    {"darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64"}
)


class DistributionError(RuntimeError):
    code: str

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class DistributionContext:
    mode: Literal["source", "npm"]
    version: str
    target: str | None
    codex_executable: Path | None = field(default=None, repr=False)
    distribution_manifest_sha256: str | None = field(default=None, repr=False)
    platform_manifest_sha256: str | None = field(default=None, repr=False)


def load_distribution_context(environ: Mapping[str, str]) -> DistributionContext:
    present = DISTRIBUTION_ENVIRONMENT.intersection(environ)
    if not present:
        return DistributionContext("source", __version__, None)
    if present != DISTRIBUTION_ENVIRONMENT:
        raise DistributionError("DISTRIBUTION_ENVIRONMENT_INVALID")
    if environ["AIZIM_DISTRIBUTION_MODE"] != "npm":
        raise DistributionError("DISTRIBUTION_MODE_INVALID")
    if environ["AIZIM_DISTRIBUTION_VERSION"] != __version__:
        raise DistributionError("DISTRIBUTION_VERSION_INVALID")
    target = environ["AIZIM_DISTRIBUTION_TARGET"]
    if target not in _TARGETS:
        raise DistributionError("DISTRIBUTION_TARGET_INVALID")
    executable = _executable(
        environ["AIZIM_CODEX_EXECUTABLE"],
        "CODEX_EXECUTABLE_INVALID",
    )
    return DistributionContext(
        "npm",
        __version__,
        target,
        executable,
        _digest(environ["AIZIM_DISTRIBUTION_MANIFEST_SHA256"]),
        _digest(environ["AIZIM_PLATFORM_MANIFEST_SHA256"]),
    )


def resolve_codex_executable(environ: Mapping[str, str]) -> Path:
    context = load_distribution_context(environ)
    if context.mode == "npm":
        if context.codex_executable is None:
            raise DistributionError("CODEX_EXECUTABLE_INVALID")
        return context.codex_executable
    value = shutil.which("codex", path=environ.get("PATH"))
    if value is None:
        raise DistributionError("CODEX_EXECUTABLE_UNAVAILABLE")
    try:
        executable = Path(value).resolve(strict=True)
    except OSError as error:
        raise DistributionError("CODEX_EXECUTABLE_UNAVAILABLE") from error
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise DistributionError("CODEX_EXECUTABLE_UNAVAILABLE")
    return executable


def without_distribution_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in environ.items()
        if name not in DISTRIBUTION_ENVIRONMENT
    }


def _digest(value: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or not value.isascii()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DistributionError("DISTRIBUTION_DIGEST_INVALID")
    return value


def _executable(value: str, code: str) -> Path:
    if type(value) is not str or not value:
        raise DistributionError(code)
    path = Path(value)
    if not path.is_absolute():
        raise DistributionError(code)
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise DistributionError(code) from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or resolved != path
        or not os.access(path, os.X_OK)
    ):
        raise DistributionError(code)
    return resolved
