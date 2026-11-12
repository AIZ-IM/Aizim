from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from integration.doctor_execution_support import (
    EXECUTION_CHECK_IDS,
    executable,
    execution_environment,
    initialized_project,
    ready_execution_services,
    ready_foundation,
)

import aizim.cli.doctor_execution_checks as doctor_execution_checks
from aizim.cli.doctor_command import run_doctor
from aizim.config import MAGPIE_VERSION, OMP_VERSION
from aizim.runtime.layout import ProjectLayout


@pytest.mark.parametrize(
    "modules",
    [
        ("aizim.security_gate", "aizim.cli.doctor_execution_checks"),
        ("aizim.cli.doctor_execution_checks", "aizim.security_gate"),
        ("aizim.cli.doctor_command", "aizim.security_gate"),
    ],
)
def test_doctor_and_security_gate_import_in_either_order(modules: tuple[str, str]) -> None:
    result = subprocess.run(
        [sys.executable, "-c", "; ".join(f"import {module}" for module in modules)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("identifier", ("omp", "magpie"))
@pytest.mark.parametrize(
    "failure", ("missing", "wrong_version", "version_prefix", "version_suffix", "nonzero_exit")
)
def test_execution_version_failure_preserves_other_readiness_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identifier: str, failure: str
) -> None:
    root = initialized_project(tmp_path)
    environment = execution_environment(tmp_path)
    ready_execution_services(monkeypatch)
    binary = Path(environment["PATH"]) / identifier
    if failure == "missing":
        binary.unlink()
    else:
        required = OMP_VERSION if identifier == "omp" else MAGPIE_VERSION
        output = {
            "wrong_version": "unsupported-fixture-version",
            "version_prefix": f"9.{required}",
            "version_suffix": f"{required}.9",
            "nonzero_exit": required,
        }[failure]
        executable(binary, output, exit_code=17 if failure == "nonzero_exit" else 0)

    checks = doctor_execution_checks.execution_checks(
        ProjectLayout.from_lean_project(root), environment
    )

    assert tuple(check.id for check in checks) == EXECUTION_CHECK_IDS
    assert {check.id: check.status for check in checks} == {
        item: "FAIL" if item == identifier else "PASS" for item in EXECUTION_CHECK_IDS
    }


@pytest.mark.parametrize("failed_check", EXECUTION_CHECK_IDS)
def test_doctor_json_rejects_each_execution_readiness_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failed_check: str,
) -> None:
    root = initialized_project(tmp_path)
    environment = execution_environment(tmp_path)
    monkeypatch.setenv("PATH", environment["PATH"])
    ready_execution_services(monkeypatch)
    ready_foundation(monkeypatch)
    secret = "private-readiness-exception-token"
    if failed_check in {"omp", "magpie"}:
        executable(Path(environment["PATH"]) / failed_check, secret, exit_code=1)
    else:
        async def fail(*_arguments: object) -> str:
            raise RuntimeError(secret)

        operation = "preflight_omp_worker" if failed_check == "omp_rpc" else "run_security_gate"
        monkeypatch.setattr(doctor_execution_checks, operation, fail)

    result = run_doctor(root, True)
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    checks = {check["id"]: check for check in document["checks"]}

    assert result == 3
    assert document["ready"] is False
    assert checks[failed_check]["status"] == "FAIL"
    assert all(
        checks[item]["status"] == "PASS"
        for item in EXECUTION_CHECK_IDS if item != failed_check
    )
    assert secret not in captured.out + captured.err
    assert all(check["status"] in {"PASS", "FAIL"} for check in document["checks"])


def test_doctor_json_is_ready_when_execution_and_foundation_are_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = initialized_project(tmp_path)
    environment = execution_environment(tmp_path)
    monkeypatch.setenv("PATH", environment["PATH"])
    ready_execution_services(monkeypatch)
    ready_foundation(monkeypatch)

    result = run_doctor(root, True)
    document = json.loads(capsys.readouterr().out)

    assert result == 0
    assert document["ready"] is True
    assert all(check["status"] == "PASS" for check in document["checks"])
    assert tuple(
        check["id"] for check in document["checks"] if check["id"] in EXECUTION_CHECK_IDS
    ) == EXECUTION_CHECK_IDS
