from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import aizim.cli.doctor_execution_checks as doctor_execution_checks
from aizim.cli import doctor_command
from aizim.config import MAGPIE_VERSION, OMP_VERSION

FIXTURE = Path(__file__).parents[1] / "fixtures" / "minimal_lean"
EXECUTION_CHECK_IDS = ("omp", "magpie", "omp_rpc", "security_probe")


def initialized_project(tmp_path: Path) -> Path:
    root = Path(shutil.copytree(FIXTURE, tmp_path / "lean-project"))
    result = subprocess.run(
        [sys.executable, "-m", "aizim", "init", str(root)],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return root


def executable(path: Path, output: str, *, exit_code: int = 0) -> Path:
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(output)}\nexit {exit_code}\n")
    path.chmod(0o755)
    return path


def execution_environment(tmp_path: Path) -> dict[str, str]:
    binary_root = tmp_path / "bin"
    binary_root.mkdir()
    executable(binary_root / "omp", f"omp/{OMP_VERSION}")
    executable(binary_root / "magpie", f"magpie {MAGPIE_VERSION}")
    return {"PATH": str(binary_root)}


def ready_execution_services(monkeypatch: pytest.MonkeyPatch) -> None:
    async def model(*_arguments: object) -> str:
        return "magpie/fixture-model"

    async def security(*_arguments: object) -> SimpleNamespace:
        return SimpleNamespace(passed=True, network_boundary="restricted host-tool transport")

    monkeypatch.setattr(doctor_execution_checks, "preflight_omp_worker", model)
    monkeypatch.setattr(doctor_execution_checks, "run_security_gate", security)


def ready_foundation(monkeypatch: pytest.MonkeyPatch) -> None:
    def command(
        identifier: str, _argv: list[str], expected: str, _cwd: Path
    ) -> doctor_command.DoctorCheck:
        return doctor_command.DoctorCheck(identifier, "PASS", expected)

    def package(identifier: str, _package: str, required: str) -> doctor_command.DoctorCheck:
        return doctor_command.DoctorCheck(identifier, "PASS", required)

    monkeypatch.setattr(doctor_command, "_command", command)
    monkeypatch.setattr(doctor_command, "_package", package)
    monkeypatch.setattr(
        doctor_command.shutil, "disk_usage", lambda _root: SimpleNamespace(free=10**15)
    )
