from __future__ import annotations

import asyncio
import secrets
import signal
import sys
from pathlib import Path

from aizim.runtime.layout import LayoutError, ProjectLayout
from aizim.runtime.state_process import StateProcessError, acquire_state_process
from aizim.state import StateService, StateServiceConfig


async def _serve(layout: ProjectLayout) -> None:
    ownership = acquire_state_process(
        layout.run_root / "state.pid", layout.run_root / "state.sock"
    )
    state: StateService | None = None
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    signals = (signal.SIGTERM, signal.SIGINT)
    try:
        state = StateService(
            StateServiceConfig(layout.root, secrets.token_urlsafe(32))
        )
        await state.start()
        for item in signals:
            loop.add_signal_handler(item, stop.set)
        await stop.wait()
        state.checkpoint()
    finally:
        for item in signals:
            loop.remove_signal_handler(item)
        try:
            if state is not None:
                await state.aclose()
        finally:
            ownership.close()


def run_state_serve(project: Path) -> int:
    try:
        layout = ProjectLayout.from_lean_project(project)
        layout.validate_runtime()
    except (LayoutError, OSError, RuntimeError, ValueError):
        print("aizim state serve: service failed", file=sys.stderr)
        return 2
    try:
        asyncio.run(_serve(layout))
    except StateProcessError:
        print("aizim state serve: service failed", file=sys.stderr)
        return 3
    except Exception:
        print("aizim state serve: service failed", file=sys.stderr)
        return 6
    return 0
