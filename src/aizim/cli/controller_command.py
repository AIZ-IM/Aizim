from __future__ import annotations

import asyncio  # noqa: ANYIO_OK - top-level adapter for the asyncio supervisor
import signal
import sys
from pathlib import Path

from aizim.config import ConfigError
from aizim.orchestration.controller_execution import ControllerExecutionError
from aizim.orchestration.controller_supervisor import ControllerSupervisor
from aizim.orchestration.resources import ResourcePolicyError
from aizim.runtime.layout import LayoutError
from aizim.runtime.state_process import StateProcessError
from aizim.state.service import StateServiceLifecycleError


async def _run_foreground(project: Path) -> None:
    from aizim.research.runner import run_research
    from aizim.runtime.layout import ProjectLayout
    from aizim.security_gate import run_security_gate

    from .state_client import load_projections

    existing = (
        load_projections(ProjectLayout.from_lean_project(project))
        if (project / ".aizim/state.sqlite3").is_file()
        else ()
    )
    if any(
        item.projection_name == "research" and item.entity_id.startswith("task:")
        for item in existing
    ):
        if not (await run_security_gate(project)).passed:
            raise ControllerExecutionError("SECURITY_GATE_FAILED")
        await run_research(project, watch=True)
        return
    supervisor = ControllerSupervisor(project)
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    try:
        for item in signals:
            loop.add_signal_handler(item, supervisor.request_stop)
        await supervisor.run()
    finally:
        for item in signals:
            loop.remove_signal_handler(item)


def run_controller_start(project: Path, foreground: bool) -> int:
    if not foreground:
        print(
            "aizim controller start: --foreground is required",
            file=sys.stderr,
        )
        return 2
    try:
        asyncio.run(_run_foreground(project))
    except (LayoutError, OSError):
        print("aizim controller start: controller failed", file=sys.stderr)
        return 2
    except (StateProcessError, StateServiceLifecycleError):
        print("aizim controller start: controller failed", file=sys.stderr)
        return 3
    except (ConfigError, ControllerExecutionError, ResourcePolicyError):
        print("aizim controller start: controller failed", file=sys.stderr)
        return 4
    except Exception:  # noqa: BROAD_EXCEPT_OK - top-level CLI safety boundary
        print("aizim controller start: controller failed", file=sys.stderr)
        return 6
    return 0
