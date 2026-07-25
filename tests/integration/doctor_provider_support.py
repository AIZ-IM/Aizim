from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import aizim.cli.doctor_provider_checks as doctor_provider_checks
from aizim.agents import BackendIdentity
from aizim.cli import doctor_command
from aizim.domain import ControllerProviderId, sha256_file
from aizim.orchestration.control_plane import configure_controller
from aizim.runtime.layout import ProjectLayout
from aizim.runtime.provider_executables import (
    ProviderExecutableError,
    ResolvedExecutable,
)
from aizim.state import StateService, StateServiceConfig

FIXTURE = Path(__file__).parents[1] / "fixtures" / "minimal_lean"
PROVIDER_CHECK_IDS = (
    "controller_configuration",
    "controller_provider",
    "controller_executable",
    "controller_auth",
    "worker_codex",
    "sandbox_exec",
)


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "aizim", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def initialized_project(tmp_path: Path) -> Path:
    root = Path(shutil.copytree(FIXTURE, tmp_path / "lean-project"))
    assert run_cli("init", str(root)).returncode == 0
    return root


def executable(path: Path, output: str) -> Path:
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n")
    path.chmod(0o755)
    return path


def persist_controller(root: Path, provider: str) -> None:
    with StateService(StateServiceConfig(root, "doctor-configure")) as state:
        configure_controller(state, ControllerProviderId(provider), None)


def descriptor(path: Path, version: str) -> ResolvedExecutable:
    canonical = path.resolve(strict=True)
    return ResolvedExecutable(canonical, version, sha256_file(canonical))


class ReadyController:
    @property
    def identity(self) -> BackendIdentity:
        return BackendIdentity("fake", "ready", "f" * 64)

    async def preflight(self) -> None:
        return None


def provider_checks(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    codex: ResolvedExecutable | ProviderExecutableError,
    claude: ResolvedExecutable | ProviderExecutableError | None = None,
) -> tuple[doctor_command.DoctorCheck, ...]:
    def resolve_codex(_environment: dict[str, str]) -> ResolvedExecutable:
        if isinstance(codex, ProviderExecutableError):
            raise codex
        return codex

    def resolve_claude(_environment: dict[str, str]) -> ResolvedExecutable:
        if claude is None:
            raise AssertionError("Claude resolution was not applicable")
        if isinstance(claude, ProviderExecutableError):
            raise claude
        return claude

    monkeypatch.setattr(doctor_provider_checks, "resolve_codex", resolve_codex)
    monkeypatch.setattr(doctor_provider_checks, "resolve_claude", resolve_claude)
    monkeypatch.setattr(
        doctor_provider_checks,
        "build_controller_backend",
        lambda *_arguments: ReadyController(),
    )
    monkeypatch.setattr(
        doctor_provider_checks,
        "_sandbox_check",
        lambda _path: doctor_command.DoctorCheck(
            "sandbox_exec",
            "PASS",
            "sandbox ready",
        ),
    )
    return doctor_provider_checks.provider_checks(
        ProjectLayout.from_lean_project(root),
        {"PATH": "/external/bin"},
    )
