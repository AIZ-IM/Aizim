from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from aizim.omp.executable import omp_version
from aizim.omp.runtime import OMP_VERSION

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


@dataclass(frozen=True, slots=True)
class LinuxSandboxDependencies:
    platform: str
    agent_executable: Path
    agent_version: Callable[[], str]
    system_bwrap: Callable[[], Path]
    bwrap_usable: Callable[[Path], bool]


class LinuxSandboxAdapter:
    def __init__(self, dependencies: LinuxSandboxDependencies) -> None:
        self._dependencies = dependencies

    @classmethod
    def for_executable(cls, executable: Path) -> LinuxSandboxAdapter:
        def system_bwrap() -> Path:
            found = shutil.which("bwrap", path=os.defpath)
            if found is None:
                raise FileNotFoundError("system bwrap is unavailable")
            return Path(found).resolve(strict=True)
        return cls(LinuxSandboxDependencies(
            sys.platform, executable, lambda: omp_version(executable),
            system_bwrap, _bwrap_usable,
        ))

    @property
    def platform_id(self) -> SandboxPlatform:
        return "linux"

    @property
    def agent_executable(self) -> Path:
        return self._dependencies.agent_executable

    @property
    def sandbox_executable(self) -> Path:
        return self._dependencies.system_bwrap()

    def validate_host(self) -> Path:
        return self._validate_host()

    def compile(self, request: SandboxRequest) -> SandboxLaunchSpec:
        bwrap = self._validate_host()
        try:
            normalized = normalize_sandbox_request(request)
            # These system trees are visible to the child; protected roots may not live there.
            for root in (Path("/usr"), Path("/bin"), Path("/sbin"), Path("/lib"), Path("/lib64")):
                if normalized.project_root == root or normalized.project_root.is_relative_to(root):
                    raise ValueError("project overlaps a system runtime root")
            spec = compile_linux_profile(bwrap, normalized)
            validate_linux_profile(spec, normalized, bwrap)
            validate_launch_spec(normalized, spec)
        except ValueError as error:
            raise SandboxHostError(str(error)) from error
        return spec

    async def launch_probe(self, request: ProbeRequest) -> ProbeReport:
        return await execute_probe(self, request, agent_version=f"omp/{OMP_VERSION}",
                                   sandbox_executable=str(self.sandbox_executable))

    def _validate_host(self) -> Path:
        dependencies = self._dependencies
        if dependencies.platform != "linux":
            raise SandboxHostError("Linux sandbox requires Linux")
        try:
            _validate_executable(dependencies.agent_executable, "omp")
            bwrap = dependencies.system_bwrap()
            _validate_executable(bwrap, "system bwrap")
            version = dependencies.agent_version()
            usable = dependencies.bwrap_usable(bwrap)
        except Exception as error:
            raise SandboxHostError("sandbox host validation failed") from error
        if version != f"omp/{OMP_VERSION}":
            raise SandboxHostError("unsupported omp version")
        if not usable:
            raise SandboxHostError("system bwrap sandbox is unavailable")
        return bwrap


def _validate_executable(path: Path, label: str) -> None:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError(f"{label} must be an absolute real path")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        raise ValueError(f"{label} is unavailable")


def _bwrap_usable(executable: Path) -> bool:
    try:
        result = subprocess.run((str(executable), "--unshare-all", "--share-net", "--new-session",
                                 "--ro-bind", "/", "/", "--", "/bin/true"),
                                check=False, capture_output=True, timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
