from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from aizim.omp.executable import omp_version
from aizim.omp.runtime import OMP_VERSION

from .macos_profile import compile_macos_profile, validate_macos_profile
from .permission_profile import canonical_directory, normalize_sandbox_request
from .probe_execution import execute_probe
from .sandbox import (
    ProbeReport,
    ProbeRequest,
    SandboxLaunchSpec,
    SandboxPlatform,
    SandboxRequest,
    validate_launch_spec,
)

_AGENT_VERSION: Final = f"omp/{OMP_VERSION}"
_SANDBOX_EXECUTABLE: Final = Path("/usr/bin/sandbox-exec")


class SandboxHostError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MacOSSandboxDependencies:
    platform: str
    agent_executable: Path
    agent_version: Callable[[], str]
    sandbox_is_apple: Callable[[], bool]
    developer_root: Callable[[], Path]


class MacOSSandboxAdapter:
    def __init__(self, dependencies: MacOSSandboxDependencies | None = None) -> None:
        self._dependencies = _default_dependencies() if dependencies is None else dependencies

    @classmethod
    def for_executable(cls, executable: Path) -> MacOSSandboxAdapter:
        return cls(
            MacOSSandboxDependencies(
                platform=sys.platform,
                agent_executable=executable,
                agent_version=lambda: omp_version(executable),
                sandbox_is_apple=_sandbox_is_apple,
                developer_root=lambda: Path(_command_output(("/usr/bin/xcode-select", "-p"))),
            )
        )

    @property
    def platform_id(self) -> SandboxPlatform:
        return "darwin"

    @property
    def agent_executable(self) -> Path:
        return self._dependencies.agent_executable

    def compile(self, request: SandboxRequest) -> SandboxLaunchSpec:
        developer_root = self._validate_host()
        try:
            normalized = normalize_sandbox_request(
                request,
                forbidden_project_roots=(
                    Path("/private/tmp"),
                    Path("/private/var/tmp"),
                ),
            )
        except ValueError as error:
            raise SandboxHostError(str(error)) from error
        spec = compile_macos_profile(
            self._dependencies.agent_executable, normalized, developer_root
        )
        validate_macos_profile(
            spec,
            normalized.project_root,
            developer_root,
            normalized.runtime_read_roots,
            provider_request=normalized,
        )
        validate_launch_spec(normalized, spec)
        return spec

    async def launch_probe(self, request: ProbeRequest) -> ProbeReport:
        return await execute_probe(
            self,
            request,
            agent_version=_AGENT_VERSION,
            sandbox_executable=str(_SANDBOX_EXECUTABLE),
        )

    def _validate_host(self) -> Path:
        dependencies = self._dependencies
        if dependencies.platform != "darwin":
            raise SandboxHostError("macOS sandbox requires Darwin")
        try:
            executable = dependencies.agent_executable
            executable_valid = (
                executable.is_absolute() and executable.resolve(strict=True) == executable
                and executable.is_file() and os.access(executable, os.X_OK)
            )
            version = dependencies.agent_version()
            signed = dependencies.sandbox_is_apple()
            developer_root = dependencies.developer_root()
        except Exception as error:
            raise SandboxHostError("sandbox host validation failed") from error
        if not executable_valid or version != _AGENT_VERSION:
            raise SandboxHostError("unsupported omp executable or version")
        if not signed:
            raise SandboxHostError("Apple sandbox executable is unavailable")
        if not developer_root.is_absolute():
            raise SandboxHostError("developer root must be absolute")
        try:
            return canonical_directory(developer_root, "developer root")
        except ValueError as error:
            raise SandboxHostError(str(error)) from error


def _default_dependencies() -> MacOSSandboxDependencies:
    executable = shutil.which("omp")
    agent = Path(executable).resolve() if executable is not None else Path("omp")
    return MacOSSandboxAdapter.for_executable(agent)._dependencies


def _command_output(argv: tuple[str, ...]) -> str:
    result = subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    return result.stdout.strip()


def host_command_output(argv: tuple[str, ...]) -> str:
    try:
        return _command_output(argv)
    except (OSError, subprocess.SubprocessError) as error:
        raise SandboxHostError("sandbox host command failed") from error


def _sandbox_is_apple() -> bool:
    if not _SANDBOX_EXECUTABLE.is_file():
        return False
    verified = subprocess.run(
        ("/usr/bin/codesign", "--verify", "--strict", str(_SANDBOX_EXECUTABLE)),
        check=False,
        capture_output=True,
        timeout=5.0,
    )
    requirement = subprocess.run(
        ("/usr/bin/codesign", "-d", "-r-", str(_SANDBOX_EXECUTABLE)),
        check=False,
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    details = requirement.stdout + requirement.stderr
    return (
        verified.returncode == 0
        and requirement.returncode == 0
        and 'identifier "com.apple.sandbox-exec"' in details
        and "anchor apple" in details
    )
