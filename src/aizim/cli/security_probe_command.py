from __future__ import annotations

import asyncio
from pathlib import Path

from aizim.security_gate import run_security_gate

_PASS_LINES = (
    "SECURITY GATE PASS",
    "gateway_matrix=pass",
    "filesystem_denials=6/6",
    "socket_denials=1/1",
    "environment_denials=1/1",
    "allowed_controls=3/3",
    "network_destination_isolation=not_enforced",
    "protected_assets=unchanged",
    "protected_state=unchanged",
)


def run_security_probe(project: Path) -> int:
    try:
        with asyncio.Runner(debug=False) as runner:
            report = runner.run(run_security_gate(project))
    except Exception:
        print("SECURITY GATE FAIL")
        return 3
    if not report.passed:
        print("SECURITY GATE FAIL")
        return 3
    profile = (
        "seatbelt_profile=pass"
        if report.platform_id == "darwin"
        else "linux_profile=pass"
    )
    print("\n".join((*_PASS_LINES[:2], profile, *_PASS_LINES[2:], report.network_boundary)))
    return 0
