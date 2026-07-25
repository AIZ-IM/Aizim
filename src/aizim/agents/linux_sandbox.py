from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .linux_profile import compile_linux_profile, validate_linux_profile
from .macos_sandbox import SandboxHostError
from .permission_profile import normalize_sandbox_request
from .probe_execution import execute_probe
from .sandbox import (
    ProbeReport,
    ProbeRequest,
    SandboxLaunchSpec,
    SandboxPlatform,
    SandboxRequest,
    validate_launch_spec,
)

_CODEX_VERSION: Final = "codex-cli 0.145.0"


@dataclass(frozen=True, slots=True)
class LinuxSandboxDependencies:
    platform: str
    codex_executable: Path
    codex_version: Callable[[], str]
    bundled_bwrap: Callable[[], Path]
    bwrap_usable: Callable[[Path], bool]


class LinuxSandboxAdapter:
    def __init__(self, dependencies: LinuxSandboxDependencies) -> None:
        self._dependencies = dependencies

    @classmethod
    def for_executable(cls, executable: Path) -> LinuxSandboxAdapter:
        from aizim.runtime.provider_executables import (
            ProviderExecutableError,
            codex_bwrap_executable,
        )

        try:
            bwrap = codex_bwrap_executable(executable)
        except ProviderExecutableError:
            bwrap = executable.parent.parent / "codex-resources" / "bwrap"
        return cls(
            LinuxSandboxDependencies(
                platform=sys.platform,
                codex_executable=executable,
                codex_version=lambda: _command_output((str(executable), "--version")),
                bundled_bwrap=lambda: bwrap,
                bwrap_usable=_bwrap_usable,
            )
        )

    @property
    def platform_id(self) -> SandboxPlatform:
        return "linux"

    @property
    def codex_executable(self) -> Path:
        return self._dependencies.codex_executable

    @property
    def sandbox_executable(self) -> Path:
        from aizim.runtime.provider_executables import (
            ProviderExecutableError,
            codex_bwrap_executable,
        )

        try:
            return codex_bwrap_executable(self.codex_executable)
        except ProviderExecutableError:
            return self.codex_executable.parent.parent / "codex-resources" / "bwrap"

    def validate_host(self) -> Path:
        return self._validate_host()

    def compile(self, request: SandboxRequest) -> SandboxLaunchSpec:
        self._validate_host()
        try:
            normalized = normalize_sandbox_request(request)
        except ValueError as error:
            raise SandboxHostError(str(error)) from error
        spec = compile_linux_profile(self.codex_executable, normalized)
        validate_linux_profile(spec, normalized, self.codex_executable)
        validate_launch_spec(normalized, spec)
        return spec

    async def launch_probe(self, request: ProbeRequest) -> ProbeReport:
        return await execute_probe(
            self,
            request,
            codex_version=_CODEX_VERSION,
            sandbox_executable=str(self.sandbox_executable),
        )

    def _validate_host(self) -> Path:
        dependencies = self._dependencies
        if dependencies.platform != "linux":
            raise SandboxHostError("Linux sandbox requires Linux")
        expected_bwrap = self.sandbox_executable
        try:
            version = dependencies.codex_version()
            bwrap = dependencies.bundled_bwrap()
            _validate_executable(dependencies.codex_executable, "Codex CLI")
            if bwrap != expected_bwrap:
                raise ValueError("unexpected bwrap path")
            _validate_executable(bwrap, "bundled bwrap")
            usable = dependencies.bwrap_usable(bwrap)
        except Exception as error:
            raise SandboxHostError("sandbox host validation failed") from error
        if version != _CODEX_VERSION:
            raise SandboxHostError("unsupported Codex CLI")
        if not usable:
            raise SandboxHostError("Codex Linux sandbox is unavailable")
        return bwrap


def _validate_executable(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not os.access(path, os.X_OK)
        or path.resolve(strict=True) != path
    ):
        raise ValueError(f"{label} is unavailable")


def _command_output(argv: tuple[str, ...]) -> str:
    result = subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    return result.stdout.strip()


def _bwrap_usable(executable: Path) -> bool:
    try:
        result = subprocess.run(
            (
                str(executable),
                "--unshare-all",
                "--new-session",
                "--ro-bind",
                "/",
                "/",
                "--",
                "/bin/true",
            ),
            check=False,
            capture_output=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
