from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aizim.agents.linux_sandbox import LinuxSandboxAdapter
from aizim.cli import doctor_command

FIXTURE = Path(__file__).parents[1] / "fixtures" / "minimal_lean"
CHECK_IDS = {
    "python",
    "uv",
    "lean",
    "lake",
    "lean_project",
    "disk_floor",
    "runtime_mode",
    "claude",
    "codex",
    "sandbox_exec",
    "lean_lsp_mcp",
    "leanclient",
    "state_service",
}


def run_cli(*args: str, environ: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "aizim", *args],
        check=False,
        capture_output=True,
        text=True,
        env=environ,
    )


def initialized_project(tmp_path: Path) -> Path:
    root = Path(shutil.copytree(FIXTURE, tmp_path / "lean-project"))
    assert run_cli("init", str(root)).returncode == 0
    return root


def executable(path: Path, output: str) -> Path:
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n")
    path.chmod(0o755)
    return path


def npm_environment(codex: Path, claude: Path) -> dict[str, str]:
    return {
        "AIZIM_DISTRIBUTION_MODE": "npm",
        "AIZIM_DISTRIBUTION_VERSION": "0.1.0",
        "AIZIM_DISTRIBUTION_TARGET": "darwin-arm64",
        "AIZIM_CLAUDE_EXECUTABLE": str(claude),
        "AIZIM_CODEX_EXECUTABLE": str(codex),
        "AIZIM_DISTRIBUTION_MANIFEST_SHA256": "1" * 64,
        "AIZIM_PLATFORM_MANIFEST_SHA256": "2" * 64,
    }


@pytest.mark.lean_integration
def test_doctor_json_has_stable_checks_and_never_echoes_secret_environment(
    tmp_path: Path,
) -> None:
    root = initialized_project(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text("#!/bin/sh\nprintf 'uv %s\\n' \"$AIZIM_TEST_TOKEN\"\n")
    fake_uv.chmod(0o755)
    environment = dict(os.environ)
    secret_values = ("api-key-value", "token-value", "credential-value")
    environment.update(
        AIZIM_TEST_API_KEY=secret_values[0],
        AIZIM_TEST_TOKEN=secret_values[1],
        AIZIM_TEST_CREDENTIAL=secret_values[2],
    )
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    result = run_cli("doctor", "--project", str(root), "--json", environ=environment)
    document = json.loads(result.stdout)

    assert result.returncode == 0
    assert document["ready"] is True
    assert {check["id"] for check in document["checks"]} == CHECK_IDS
    assert {check["status"] for check in document["checks"]} <= {"PASS", "WARN", "FAIL"}
    assert all(secret not in result.stdout + result.stderr for secret in secret_values)


@pytest.mark.lean_integration
def test_doctor_human_output_ends_ready(tmp_path: Path) -> None:
    root = initialized_project(tmp_path)

    result = run_cli("doctor", "--project", str(root))

    assert result.returncode == 0
    assert result.stdout.splitlines()[-1] == "READY"
    assert all(line == line.rstrip() for line in result.stdout.splitlines())
    assert all(line.split(maxsplit=1)[0] in {"PASS", "WARN", "FAIL", "READY"}
               for line in result.stdout.splitlines())


def test_doctor_wrong_foundation_pin_is_not_ready_without_leaking_config(
    tmp_path: Path,
) -> None:
    root = initialized_project(tmp_path)
    config = root / ".aizim" / "config.toml"
    config.write_text(config.read_text().replace("0.28.1", "0.28.0"))

    result = run_cli("doctor", "--project", str(root), "--json")
    document = json.loads(result.stdout)

    assert result.returncode == 2
    assert document["ready"] is False
    assert any(check["status"] == "FAIL" for check in document["checks"])
    assert "0.28.0" not in result.stdout


def test_doctor_invalid_project_keeps_the_stable_check_ids(tmp_path: Path) -> None:
    result = run_cli("doctor", "--project", str(tmp_path), "--json")
    document = json.loads(result.stdout)

    assert result.returncode == 2
    assert document["ready"] is False
    assert {check["id"] for check in document["checks"]} == CHECK_IDS


def test_doctor_readiness_failure_uses_exit_three(tmp_path: Path) -> None:
    root = initialized_project(tmp_path)
    empty_path = tmp_path / "empty-bin"
    empty_path.mkdir()
    environment = dict(os.environ, PATH=str(empty_path))

    result = run_cli("doctor", "--project", str(root), "--json", environ=environment)
    document = json.loads(result.stdout)

    assert result.returncode == 3
    assert document["ready"] is False


def test_doctor_npm_mode_uses_the_injected_codex_instead_of_path(tmp_path: Path) -> None:
    root = initialized_project(tmp_path)
    wrong_bin = tmp_path / "wrong-bin"
    wrong_bin.mkdir()
    executable(wrong_bin / "codex", "codex-cli 0.0.0")
    executable(wrong_bin / "claude", "0.0.0 (Claude Code)")
    injected = executable(tmp_path / "packaged-codex", "codex-cli 0.145.0")
    claude_arguments = tmp_path / "claude-arguments"
    injected_claude = tmp_path / "packaged-claude"
    injected_claude.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > '{claude_arguments}'\n"
        "printf '%s\\n' '2.1.218 (Claude Code)'\n",
    )
    injected_claude.chmod(0o755)
    environment = dict(os.environ)
    environment.update(npm_environment(injected, injected_claude))
    environment["PATH"] = f"{wrong_bin}:{environment['PATH']}"

    result = run_cli("doctor", "--project", str(root), "--json", environ=environment)
    document = json.loads(result.stdout)
    codex = next(check for check in document["checks"] if check["id"] == "codex")
    claude = next(check for check in document["checks"] if check["id"] == "claude")
    uv = next(check for check in document["checks"] if check["id"] == "uv")

    assert codex["status"] == "PASS"
    assert claude == {
        "id": "claude",
        "status": "PASS",
        "detail": "2.1.218 (Claude Code)",
    }
    assert uv == {
        "id": "uv",
        "status": "PASS",
        "detail": "bundled uv 0.11.31",
    }
    assert "0.145.0" in codex["detail"]
    assert "0.0.0" not in result.stdout
    assert claude_arguments.read_text() == "--version\n"
    assert str(injected) not in result.stdout + result.stderr
    assert str(injected_claude) not in result.stdout + result.stderr


def test_doctor_source_mode_still_resolves_codex_from_path(tmp_path: Path) -> None:
    root = initialized_project(tmp_path)
    source_bin = tmp_path / "source-bin"
    source_bin.mkdir()
    executable(source_bin / "codex", "codex-cli 0.145.0")
    executable(source_bin / "claude", "2.1.218 (Claude Code)")
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("AIZIM_DISTRIBUTION_")
        and name != "AIZIM_CLAUDE_EXECUTABLE"
        and name != "AIZIM_CODEX_EXECUTABLE"
    }
    environment["PATH"] = f"{source_bin}:{environment['PATH']}"

    result = run_cli("doctor", "--project", str(root), "--json", environ=environment)
    document = json.loads(result.stdout)
    codex = next(check for check in document["checks"] if check["id"] == "codex")
    claude = next(check for check in document["checks"] if check["id"] == "claude")

    assert codex["status"] == "PASS"
    assert claude["status"] == "PASS"
    assert claude["detail"] == "2.1.218 (Claude Code)"
    assert "0.145.0" in codex["detail"]


def test_doctor_reports_linux_sandbox_only_after_host_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex = executable(tmp_path / "packaged-codex", "codex-cli 0.145.0")
    validated: list[Path] = []

    def validate_host(adapter: LinuxSandboxAdapter) -> Path:
        validated.append(adapter.codex_executable)
        return tmp_path / "bwrap"

    monkeypatch.setattr(
        doctor_command.LinuxSandboxAdapter,
        "validate_host",
        validate_host,
    )

    check = doctor_command._sandbox_check(codex.resolve(), "linux")

    assert check == doctor_command.DoctorCheck(
        "sandbox_exec",
        "PASS",
        "Codex Linux sandbox available",
    )
    assert validated == [codex.resolve()]


def test_doctor_fails_closed_when_linux_sandbox_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex = executable(tmp_path / "packaged-codex", "codex-cli 0.145.0")

    def fail_host(_adapter: LinuxSandboxAdapter) -> Path:
        raise OSError("bwrap unavailable")

    monkeypatch.setattr(
        doctor_command.LinuxSandboxAdapter,
        "validate_host",
        fail_host,
    )

    check = doctor_command._sandbox_check(codex.resolve(), "linux")

    assert check == doctor_command.DoctorCheck(
        "sandbox_exec",
        "FAIL",
        "sandbox mechanism is unavailable",
    )
