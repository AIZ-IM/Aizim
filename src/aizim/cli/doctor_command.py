from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from aizim.agents.launcher import AgentLaunchError, HostCommandSpec, run_host_command
from aizim.config import (
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
)
from aizim.runtime.layout import LayoutError, ProjectLayout

from .doctor_provider_checks import DoctorCheck, provider_checks
from .state_client import StateClientError, check_state_health

_CHECK_IDS = (
    "python",
    "uv",
    "lean",
    "lake",
    "lean_project",
    "disk_floor",
    "runtime_mode",
    "controller_configuration",
    "controller_provider",
    "controller_executable",
    "controller_auth",
    "worker_codex",
    "sandbox_exec",
    "lean_lsp_mcp",
    "leanclient",
    "state_service",
)
_SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")
_BUNDLED_UV_VERSION = "0.11.31"


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


def _uv(layout: ProjectLayout) -> DoctorCheck:
    try:
        context = load_distribution_context(os.environ)
    except DistributionError:
        return DoctorCheck("uv", "FAIL", "uv distribution is invalid")
    if context.mode == "npm":
        return DoctorCheck("uv", "PASS", f"bundled uv {_BUNDLED_UV_VERSION}")
    return _command("uv", ["uv", "--version"], "uv ", layout.root)


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
    providers = provider_checks(layout, os.environ)
    return (
        _check("python", python_ready, sys.version.split()[0], "Python 3.12-3.14 is required"),
        _uv(layout),
        _command("lean", ["lake", "env", "lean", "--version"], "4.32.1", layout.root),
        _command("lake", ["lake", "--version"], "Lake version", layout.root),
        DoctorCheck("lean_project", "PASS", str(layout.root)),
        _check("disk_floor", free >= floor, f"{free} bytes free", "free-space floor not met"),
        runtime,
        *providers,
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
