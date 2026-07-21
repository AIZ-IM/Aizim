from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from aizim.config import (
    CODEX_CLI_VERSION,
    LEAN_LSP_MCP_VERSION,
    LEANCLIENT_VERSION,
    MIN_FREE_DISK_BYTES,
    AizimConfig,
    load_config,
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
    "codex",
    "sandbox_exec",
    "lean_lsp_mcp",
    "leanclient",
    "state_service",
)


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    id: str
    status: str
    detail: str


def _check(identifier: str, passed: bool, success: str, failure: str) -> DoctorCheck:
    return DoctorCheck(identifier, "PASS" if passed else "FAIL", success if passed else failure)


def _command(identifier: str, argv: list[str], expected: str, cwd: Path) -> DoctorCheck:
    executable = shutil.which(argv[0])
    if executable is None:
        return DoctorCheck(identifier, "FAIL", "required executable is unavailable")
    try:
        result = subprocess.run(
            [executable, *argv[1:]],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return DoctorCheck(identifier, "FAIL", "version check failed")
    output = result.stdout + result.stderr
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


def doctor_checks(layout: ProjectLayout) -> tuple[DoctorCheck, ...]:
    config, runtime = _configuration(layout)
    floor = MIN_FREE_DISK_BYTES if config is None else config.resources.min_free_disk_bytes
    free = shutil.disk_usage(layout.root).free
    python_ready = (3, 12) <= sys.version_info[:2] < (3, 15)
    sandbox = Path("/usr/bin/sandbox-exec")
    try:
        check_state_health(layout)
        state = DoctorCheck("state_service", "PASS", "schema 1 ready")
    except StateClientError:
        state = DoctorCheck("state_service", "FAIL", "state service is unavailable")
    return (
        _check("python", python_ready, sys.version.split()[0], "Python 3.12-3.14 is required"),
        _command("uv", ["uv", "--version"], "uv ", layout.root),
        _command("lean", ["lake", "env", "lean", "--version"], "4.32.0", layout.root),
        _command("lake", ["lake", "--version"], "Lake version", layout.root),
        DoctorCheck("lean_project", "PASS", str(layout.root)),
        _check("disk_floor", free >= floor, f"{free} bytes free", "free-space floor not met"),
        runtime,
        _command("codex", ["codex", "--version"], CODEX_CLI_VERSION, layout.root),
        _check(
            "sandbox_exec",
            sandbox.is_file() and os.access(sandbox, os.X_OK),
            "sandbox-exec available",
            "sandbox mechanism is unavailable",
        ),
        _package("lean_lsp_mcp", "lean-lsp-mcp", LEAN_LSP_MCP_VERSION),
        _package("leanclient", "leanclient", LEANCLIENT_VERSION),
        state,
    )


def run_doctor(project: Path, as_json: bool) -> int:
    try:
        layout = ProjectLayout.from_lean_project(project)
        layout.validate_runtime()
        checks = doctor_checks(layout)
    except (LayoutError, OSError, RuntimeError, ValueError):
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
    return 0 if ready else 2
