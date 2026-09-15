from __future__ import annotations

from pathlib import Path

import pytest
from integration.doctor_provider_support import executable

import aizim.cli.doctor_provider_checks as doctor_provider_checks
from aizim.agents.linux_sandbox import LinuxSandboxAdapter
from aizim.cli import doctor_command


def test_linux_sandbox_passes_only_after_host_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex = executable(tmp_path / "codex", "codex-cli 0.154.0")
    validated: list[Path] = []

    def validate_host(adapter: LinuxSandboxAdapter) -> Path:
        validated.append(adapter.codex_executable)
        return tmp_path / "bwrap"

    monkeypatch.setattr(
        doctor_provider_checks.LinuxSandboxAdapter,
        "validate_host",
        validate_host,
    )

    check = doctor_provider_checks._sandbox_check(codex.resolve(), "linux")

    assert check == doctor_command.DoctorCheck(
        "sandbox_exec",
        "PASS",
        "Codex Linux sandbox available",
    )
    assert validated == [codex.resolve()]


def test_linux_sandbox_fails_closed_when_host_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex = executable(tmp_path / "codex", "codex-cli 0.154.0")

    def fail_host(_adapter: LinuxSandboxAdapter) -> Path:
        raise OSError("bwrap unavailable")

    monkeypatch.setattr(
        doctor_provider_checks.LinuxSandboxAdapter,
        "validate_host",
        fail_host,
    )

    check = doctor_provider_checks._sandbox_check(codex.resolve(), "linux")

    assert check == doctor_command.DoctorCheck(
        "sandbox_exec",
        "FAIL",
        "sandbox mechanism is unavailable",
    )
