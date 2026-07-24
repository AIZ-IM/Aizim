from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from aizim.agents.launcher import AgentLaunchError, HostCommandSpec, run_host_command
from aizim.agents.linux_sandbox import LinuxSandboxAdapter
from aizim.config import (
    CODEX_CLI_VERSION,
    LEAN_LSP_MCP_VERSION,
    LEANCLIENT_VERSION,
    MIN_FREE_DISK_BYTES,
    AizimConfig,
    load_config,
)
from aizim.runtime.distribution import (
    DISTRIBUTION_ENVIRONMENT,
    DistributionError,
    load_distribution_context,
    resolve_claude_executable,
    resolve_codex_executable,
)
from aizim.runtime.layout import LayoutError, ProjectLayout

from .state_client import StateClientError, check_state_health

_CHECK_IDS = (
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
)
_SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")
_BUNDLED_UV_VERSION = "0.11.31"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    id: str
    status: str
    detail: str


def _check(identifier: str, passed: bool, success: str, failure: str) -> DoctorCheck:
    return DoctorCheck(identifier, "PASS" if passed else "FAIL", success if passed else failure)


def scrubbed_command_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if environ is None else environ
    return {
        name: value
        for name, value in source.items()
        if name not in DISTRIBUTION_ENVIRONMENT
        and not any(marker in name.upper() for marker in _SECRET_MARKERS)
    }


def _command(identifier: str, argv: list[str], expected: str, cwd: Path) -> DoctorCheck:
    environment = scrubbed_command_environment()
    candidate = Path(argv[0])
    executable = (
        str(candidate)
        if candidate.is_absolute()
        else shutil.which(argv[0], path=environment.get("PATH"))
    )
    if executable is None:
        return DoctorCheck(identifier, "FAIL", "required executable is unavailable")
    try:
        result = run_host_command(
            HostCommandSpec(
                argv=(executable, *argv[1:]),
                cwd=cwd,
                environment=environment,
            )
        )
    except (AgentLaunchError, OSError, ValueError):
        return DoctorCheck(identifier, "FAIL", "version check failed")
    output = (result.stdout + result.stderr).decode(errors="replace")
    detail = next((line.strip() for line in output.splitlines() if line.strip()), expected)
    passed = result.returncode == 0 and expected in output
    return _check(identifier, passed, detail, "required version is unavailable")


def _package(identifier: str, package: str, required: str) -> DoctorCheck:
    try:
        actual = version(package)
    except PackageNotFoundError:
        return DoctorCheck(identifier, "FAIL", "required package is unavailable")
    return _check(identifier, actual == required, required, "required package pin is unavailable")


def _configuration(layout: ProjectLayout) -> tuple[AizimConfig | None, DoctorCheck]:
    try:
        config = load_config(layout.root, environ={})
    except (OSError, RuntimeError, ValueError):
        return None, DoctorCheck("runtime_mode", "FAIL", "configuration is invalid")
    return config, DoctorCheck("runtime_mode", "PASS", config.run.lean_runtime.value)


def _codex(layout: ProjectLayout) -> DoctorCheck:
    try:
        executable = resolve_codex_executable(os.environ)
    except DistributionError:
        return DoctorCheck("codex", "FAIL", "Codex distribution is invalid")
    return _command(
        "codex",
        [str(executable), "--version"],
        CODEX_CLI_VERSION,
        layout.root,
    )


def _claude(layout: ProjectLayout) -> DoctorCheck:
    expected = "2.1.218 (Claude Code)"
    try:
        executable = resolve_claude_executable(os.environ)
    except DistributionError:
        return DoctorCheck("claude", "FAIL", "Claude distribution is invalid")
    try:
        result = run_host_command(
            HostCommandSpec(
                argv=(str(executable), "--version"),
                cwd=layout.root,
                environment=scrubbed_command_environment(),
            )
        )
    except (AgentLaunchError, OSError, ValueError):
        return DoctorCheck("claude", "FAIL", "version check failed")
    output = (result.stdout + result.stderr).decode(errors="replace").strip()
    return _check(
        "claude",
        result.returncode == 0 and output == expected,
        expected,
        "required version is unavailable",
    )


def _uv(layout: ProjectLayout) -> DoctorCheck:
    try:
        context = load_distribution_context(os.environ)
    except DistributionError:
        return DoctorCheck("uv", "FAIL", "uv distribution is invalid")
    if context.mode == "npm":
        return DoctorCheck("uv", "PASS", f"bundled uv {_BUNDLED_UV_VERSION}")
    return _command("uv", ["uv", "--version"], "uv ", layout.root)


def _sandbox_check(
    executable: Path,
    platform: str | None = None,
) -> DoctorCheck:
    host = sys.platform if platform is None else platform
    if host == "darwin":
        sandbox = Path("/usr/bin/sandbox-exec")
        return _check(
            "sandbox_exec",
            sandbox.is_file() and os.access(sandbox, os.X_OK),
            "sandbox-exec available",
            "sandbox mechanism is unavailable",
        )
    if host == "linux":
        try:
            LinuxSandboxAdapter.for_executable(executable).validate_host()
        except (OSError, RuntimeError, ValueError):
            return DoctorCheck(
                "sandbox_exec",
                "FAIL",
                "sandbox mechanism is unavailable",
            )
        return DoctorCheck(
            "sandbox_exec",
            "PASS",
            "Codex Linux sandbox available",
        )
    return DoctorCheck("sandbox_exec", "FAIL", "sandbox mechanism is unavailable")


def _sandbox(layout: ProjectLayout) -> DoctorCheck:
    try:
        executable = resolve_codex_executable(os.environ)
    except DistributionError:
        return DoctorCheck("sandbox_exec", "FAIL", "sandbox mechanism is unavailable")
    return _sandbox_check(executable)


def doctor_checks(layout: ProjectLayout) -> tuple[DoctorCheck, ...]:
    config, runtime = _configuration(layout)
    floor = MIN_FREE_DISK_BYTES if config is None else config.resources.min_free_disk_bytes
    free = shutil.disk_usage(layout.root).free
    python_ready = (3, 12) <= sys.version_info[:2] < (3, 15)
    try:
        check_state_health(layout)
        state = DoctorCheck("state_service", "PASS", "schema 1 ready")
    except StateClientError:
        state = DoctorCheck("state_service", "FAIL", "state service is unavailable")
    return (
        _check("python", python_ready, sys.version.split()[0], "Python 3.12-3.14 is required"),
        _uv(layout),
        _command("lean", ["lake", "env", "lean", "--version"], "4.32.1", layout.root),
        _command("lake", ["lake", "--version"], "Lake version", layout.root),
        DoctorCheck("lean_project", "PASS", str(layout.root)),
        _check("disk_floor", free >= floor, f"{free} bytes free", "free-space floor not met"),
        runtime,
        _claude(layout),
        _codex(layout),
        _sandbox(layout),
        _package("lean_lsp_mcp", "lean-lsp-mcp", LEAN_LSP_MCP_VERSION),
        _package("leanclient", "leanclient", LEANCLIENT_VERSION),
        state,
    )


def run_doctor(project: Path, as_json: bool) -> int:
    invalid = False
    try:
        layout = ProjectLayout.from_lean_project(project)
        layout.validate_runtime()
        checks = doctor_checks(layout)
    except (LayoutError, OSError, RuntimeError, ValueError):
        invalid = True
        checks = tuple(
            DoctorCheck(identifier, "FAIL", "project readiness is unavailable")
            for identifier in _CHECK_IDS
        )
    ready = all(check.status != "FAIL" for check in checks)
    if as_json:
        document = {"checks": [asdict(check) for check in checks], "ready": ready}
        print(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        for check in checks:
            print(f"{check.status} {check.id} {check.detail}")
        print("READY" if ready else "NOT READY")
    return 0 if ready else 2 if invalid else 3
