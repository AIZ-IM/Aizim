from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass

from aizim.agents.launcher import HostCommandSpec, run_host_command
from aizim.agents.omp_backend import preflight_omp_worker
from aizim.config import MAGPIE_VERSION, OMP_VERSION
from aizim.runtime.layout import ProjectLayout
from aizim.security_gate import run_security_gate


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    id: str
    status: str
    detail: str


def _version(
    name: str, required: str, layout: ProjectLayout, environment: Mapping[str, str]
) -> DoctorCheck:
    executable = shutil.which(name, path=environment.get("PATH"))
    if executable is None:
        return DoctorCheck(name, "FAIL", f"{name} is unavailable")
    try:
        result = run_host_command(HostCommandSpec(
            argv=(executable, "--version"), cwd=layout.root, environment=dict(environment),
        ))
        output = (result.stdout + result.stderr).decode(errors="replace")
        passed = result.returncode == 0 and re.search(
            rf"(?<![\d.]){re.escape(required)}(?![\d.])", output
        ) is not None
    except Exception:
        passed = False
    return DoctorCheck(
        name,
        "PASS" if passed else "FAIL",
        required if passed else "required version is unavailable",
    )


async def _rpc_and_security(
    layout: ProjectLayout, environment: Mapping[str, str]
) -> tuple[DoctorCheck, ...]:
    try:
        model = await preflight_omp_worker(layout.root, environment)
        rpc = DoctorCheck("omp_rpc", "PASS", f"selected model: {model}")
    except Exception:
        rpc = DoctorCheck(
            "omp_rpc", "FAIL", "restricted RPC or selected magpie model is unavailable"
        )
    try:
        report = await run_security_gate(layout.root)
        security = DoctorCheck(
            "security_probe",
            "PASS" if report.passed else "FAIL",
            report.network_boundary if report.passed else "security probe failed",
        )
    except Exception:
        security = DoctorCheck("security_probe", "FAIL", "security probe is unavailable")
    return rpc, security


def execution_checks(
    layout: ProjectLayout, environment: Mapping[str, str]
) -> tuple[DoctorCheck, ...]:
    return (
        _version("omp", OMP_VERSION, layout, environment),
        _version("magpie", MAGPIE_VERSION, layout, environment),
        *asyncio.run(_rpc_and_security(layout, environment)),
    )
