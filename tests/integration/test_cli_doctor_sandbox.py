from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from integration.doctor_execution_support import (
    execution_environment,
    initialized_project,
    ready_execution_services,
    ready_foundation,
)

import aizim.cli.doctor_execution_checks as doctor_execution_checks
from aizim.cli.doctor_command import run_doctor
from aizim.runtime.layout import ProjectLayout


@pytest.mark.parametrize("unavailable", (False, True))
def test_security_readiness_fails_for_failed_or_unavailable_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unavailable: bool
) -> None:
    root = initialized_project(tmp_path)
    environment = execution_environment(tmp_path)
    ready_execution_services(monkeypatch)

    async def probe(*_arguments: object) -> SimpleNamespace:
        if unavailable:
            raise OSError("private-sandbox-host-error")
        return SimpleNamespace(passed=False, network_boundary="unavailable")

    monkeypatch.setattr(doctor_execution_checks, "run_security_gate", probe)

    checks = {
        check.id: check
        for check in doctor_execution_checks.execution_checks(
            ProjectLayout.from_lean_project(root), environment
        )
    }

    assert checks["security_probe"].status == "FAIL"
    assert checks["security_probe"].detail == (
        "security probe is unavailable" if unavailable else "security probe failed"
    )
    assert checks["omp_rpc"].status == "PASS"
    assert "private-sandbox-host-error" not in repr(checks)


def test_failed_security_probe_makes_human_doctor_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = initialized_project(tmp_path)
    environment = execution_environment(tmp_path)
    monkeypatch.setenv("PATH", environment["PATH"])
    ready_execution_services(monkeypatch)
    ready_foundation(monkeypatch)

    async def failed_probe(*_arguments: object) -> SimpleNamespace:
        return SimpleNamespace(passed=False, network_boundary="unavailable")

    monkeypatch.setattr(doctor_execution_checks, "run_security_gate", failed_probe)

    result = run_doctor(root, False)
    lines = capsys.readouterr().out.splitlines()

    assert result == 3
    assert "FAIL security_probe security probe failed" in lines
    assert lines[-1] == "NOT READY"
    assert all(not line.startswith("SKIP ") for line in lines)
