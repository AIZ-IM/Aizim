from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from aizim import __version__

from .provider_executables import (
    ProviderExecutableError,
    codex_ripgrep_executable,
    discover_claude_executable,
    discover_codex_executable,
)

DISTRIBUTION_ENVIRONMENT: Final[frozenset[str]] = frozenset(
    {
        "AIZIM_DISTRIBUTION_MODE",
        "AIZIM_DISTRIBUTION_VERSION",
        "AIZIM_DISTRIBUTION_TARGET",
        "AIZIM_DISTRIBUTION_MANIFEST_SHA256",
        "AIZIM_PLATFORM_MANIFEST_SHA256",
    }
)
_TARGETS: Final[frozenset[str]] = frozenset({"darwin-arm64", "linux-arm64", "linux-x64"})


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
    return DistributionContext(
        "npm",
        __version__,
        target,
        _digest(environ["AIZIM_DISTRIBUTION_MANIFEST_SHA256"]),
        _digest(environ["AIZIM_PLATFORM_MANIFEST_SHA256"]),
    )


def resolve_claude_executable(environ: Mapping[str, str]) -> Path:
    try:
        return discover_claude_executable(environ)
    except ProviderExecutableError as error:
        raise DistributionError(error.code) from error


def resolve_codex_executable(environ: Mapping[str, str]) -> Path:
    try:
        return discover_codex_executable(environ)
    except ProviderExecutableError as error:
        raise DistributionError(error.code) from error


def resolve_ripgrep_executable(environ: Mapping[str, str]) -> Path:
    import shutil

    if (value := shutil.which("rg", path=environ.get("PATH"))) is not None:
        try:
            executable = Path(value).resolve(strict=True)
        except OSError as error:
            raise DistributionError("RIPGREP_EXECUTABLE_UNAVAILABLE") from error
        if executable.is_file() and os.access(executable, os.X_OK):
            return executable
    try:
        codex = resolve_codex_executable(environ)
        return codex_ripgrep_executable(codex)
    except (DistributionError, ProviderExecutableError) as error:
        raise DistributionError("RIPGREP_EXECUTABLE_UNAVAILABLE") from error


def without_distribution_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in environ.items() if name not in DISTRIBUTION_ENVIRONMENT}


def _digest(value: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or not value.isascii()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DistributionError("DISTRIBUTION_DIGEST_INVALID")
    return value
