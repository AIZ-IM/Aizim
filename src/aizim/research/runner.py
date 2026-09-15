from __future__ import annotations

import asyncio
import os
import shutil
import signal
from datetime import UTC, datetime
from pathlib import Path

from aizim.config import load_config
from aizim.domain import ControllerProviderId
from aizim.domain.serialization import JsonValue
from aizim.orchestration.codex_worker import create_codex_backend, preflight_codex_worker
from aizim.orchestration.controller_dispatcher import record_controller_stop
from aizim.orchestration.controller_lifecycle import recover_unclean_controller, start_controller
from aizim.orchestration.controller_providers import (
    build_controller_backend,
    resolve_controller_runtime,
)
from aizim.orchestration.resources import ResourceGovernor
from aizim.orchestration.worker_host import WorkerExecutionHost
from aizim.runtime.layout import ProjectLayout
from aizim.runtime.state_process import acquire_state_process
from aizim.state import StateService, StateServiceConfig
from aizim.state.events import MonotoneUlidFactory

from .engine import LeanExecutor, ResearchEngine
from .records import ResearchError, object_value, records, save, text
from .search import lean_search


async def run_research(
    project: Path, *, watch: bool = False, max_seconds: int | None = None
) -> dict[str, int]:
    import json

    layout = ProjectLayout.from_lean_project(project)
    layout.validate_runtime()
    config = load_config(layout.root)
    if not config.model:
        raise ResearchError("WORKER_MODEL_REQUIRED_SET_AIZIM_MODEL")
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
    started = False
    run_record: dict[str, JsonValue] = {
        "id": session,
        "status": "running",
        "model": config.model,
        "participation": config.run.participation.value,
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": "",
        "reason": "",
    }
    try:
        state = StateService(StateServiceConfig(layout.root, session))
        await state.start()
        recover_unclean_controller(state)
        configured = state.query_projection("controller", "primary")
        if configured is None:
            raise ResearchError("CONTROLLER_NOT_CONFIGURED")
        settings = object_value(object_value(json.loads(configured.state_json))["payload"])
        environment = dict(os.environ)
        runtime = resolve_controller_runtime(
            ControllerProviderId(text(settings["provider"])), environment
        )
        configured_model = settings.get("model")
        controller_model = None if configured_model is None else text(configured_model)

        def controller():
            return build_controller_backend(runtime, layout.root, controller_model, environment)

        await controller().preflight()
        await preflight_codex_worker(layout.root, runtime.codex, environment)
        host = await WorkerExecutionHost.open(
            state, layout.root, layout.root, governor, session, continue_on_failure=True
        )
        await host.prewarm()
        start_controller(
            state,
            session_id=session,
            controller_version=configured.version,
            provider=runtime.provider,
            backend_version=runtime.controller.version,
            executable_hash=runtime.controller.sha256,
        )
        started = True

        def validate_control() -> None:
            current = state.query_projection("controller", "primary")
            if current is None or current.version != configured.version:
                raise ResearchError("CONTROLLER_CONFIGURATION_CHANGED_RESTART_REQUIRED")

        executor = LeanExecutor(
            state,
            layout.root,
            host,
            create_codex_backend(runtime.codex, environment),
            config.model,
            controller,
        )
        engine = ResearchEngine(
            state,
            executor,
            session,
            config.model,
            workers=min(config.resources.max_proof_workers, config.resources.scratch_slots),
            controller_model=controller_model or text(settings["provider"]) + "-default",
            validate_control=validate_control,
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
                        if started:
                            reason = (
                                "RESEARCH_IDLE"
                                if run_record["status"] == "finished"
                                else "TIME_LIMIT"
                                if run_record["reason"] == "time_limit"
                                else "CONTROLLER_FAILED"
                                if run_record["reason"]
                                else "OPERATOR_SIGNAL"
                            )
                            record_controller_stop(state, session, reason)
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
