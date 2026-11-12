from __future__ import annotations

import asyncio
import os
import shutil
import signal
from datetime import UTC, datetime
from pathlib import Path

from aizim.agents.omp_backend import create_omp_backend, preflight_omp_worker
from aizim.config import load_config
from aizim.domain.serialization import JsonValue
from aizim.orchestration.resources import ResourceGovernor
from aizim.orchestration.worker_host import WorkerExecutionHost
from aizim.runtime.layout import ProjectLayout
from aizim.runtime.state_process import acquire_state_process
from aizim.state import StateService, StateServiceConfig
from aizim.state.events import MonotoneUlidFactory

from .engine import RESEARCH_GATEWAY_LIMITS, LeanExecutor, ResearchEngine
from .records import records, save, text
from .search import lean_search


async def run_research(
    project: Path, *, watch: bool = False, max_seconds: int | None = None
) -> dict[str, int]:
    layout = ProjectLayout.from_lean_project(project)
    layout.validate_runtime()
    config = load_config(layout.root)
    governor = ResourceGovernor(
        config.resources, disk_free=lambda path: shutil.disk_usage(path).free
    )
    governor.validate(layout.root, config.run.lean_runtime)
    session = "research-" + MonotoneUlidFactory()().lower()
    ownership = acquire_state_process(layout.run_root / "state.pid", layout.run_root / "state.sock")
    state: StateService | None = None
    host: WorkerExecutionHost | None = None
    engine: ResearchEngine | None = None
    registered: list[signal.Signals] = []
    run_record: dict[str, JsonValue] = {
        "id": session,
        "status": "running",
        "model": "pending",
        "participation": config.run.participation.value,
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": "",
        "reason": "",
    }
    try:
        state = StateService(StateServiceConfig(layout.root, session))
        await state.start()
        environment = dict(os.environ)
        model = await preflight_omp_worker(layout.root, environment)
        run_record["model"] = model
        host = await WorkerExecutionHost.open(
            state,
            layout.root,
            layout.root,
            governor,
            session,
            continue_on_failure=True,
            gateway_limits=RESEARCH_GATEWAY_LIMITS,
        )
        await host.prewarm()
        executor = LeanExecutor(
            state,
            layout.root,
            host,
            create_omp_backend(environment),
        )
        engine = ResearchEngine(
            state,
            executor,
            session,
            model,
            workers=min(config.resources.max_proof_workers, config.resources.scratch_slots),
            context_search=lambda query: lean_search(layout.root, query, limit=8),
        )
        save(state, "run", session, run_record, actor="research_supervisor", run_id=session)
        loop = asyncio.get_running_loop()
        for item in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(item, engine.request_stop)
            registered.append(item)
        try:
            async with asyncio.timeout(max_seconds):
                await engine.run(watch=watch)
        except TimeoutError:
            engine.request_stop()
            run_record["reason"] = "time_limit"
        if engine.reason:
            run_record["reason"] = engine.reason
        run_record["status"] = "interrupted" if engine.stop.is_set() else "finished"
        result: dict[str, int] = {}
        for task in records(state, "task"):
            status = "paused" if task["paused"] else text(task["status"])
            result[status] = result.get(status, 0) + 1
        return result
    except BaseException:
        run_record["status"], run_record["reason"] = "interrupted", "run_interrupted"
        raise
    finally:
        for item in registered:
            asyncio.get_running_loop().remove_signal_handler(item)
        try:
            if host is not None:
                await host.aclose()
        finally:
            try:
                if state is not None:
                    run_record["finished_at"] = datetime.now(UTC).isoformat()
                    try:
                        save(
                            state,
                            "run",
                            session,
                            run_record,
                            actor="research_supervisor",
                            run_id=session,
                        )
                    finally:
                        await state.aclose()
            finally:
                ownership.close()
