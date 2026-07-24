from __future__ import annotations

import asyncio  # noqa: ANYIO_OK - composes existing asyncio RPC and worker lifecycles
import json
import re
import secrets
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from aizim.agents import AgentBackend
from aizim.config import load_config
from aizim.domain.serialization import JsonValue
from aizim.lean import DocumentBroker
from aizim.lean.project import smoke_base_epoch
from aizim.orchestration.codex_worker import create_codex_backend
from aizim.runtime.layout import ProjectLayout
from aizim.runtime.state_process import acquire_state_process
from aizim.state import (
    AppendEventCommand,
    ProjectionRecord,
    StateDependencies,
    StateService,
    StateServiceConfig,
)

from .control_plane import ControllerProvider
from .controller_backend import ControllerBackend
from .controller_dispatcher import (
    ControllerDispatcher,
    ControllerDispatcherDependencies,
    ControllerRun,
    claim_next,
    controller_context,
)
from .controller_execution import (
    ControllerExecutionError,
    interrupt_nonterminal_executions,
)
from .controller_lifecycle import recover_unclean_controller, start_controller
from .resources import ResourceGovernor

_CONTROLLER_ID = "primary"
_SESSION_ID = re.compile(r"controller-[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class ControllerSupervisorDependencies:
    controller_backend: Callable[[ControllerProvider, str | None], ControllerBackend]
    worker_backend: Callable[[], AgentBackend]
    worker_preflight: Callable[[], Awaitable[None]]
    session_ids: Callable[[], str]
    execution_ids: Callable[[], str]
    directive_ids: Callable[[], str]


class ControllerSupervisor:
    def __init__(
        self,
        project: Path,
        dependencies: ControllerSupervisorDependencies | None = None,
    ) -> None:
        self._project = project
        self._dependencies = _default_dependencies() if dependencies is None else dependencies
        self._stop, self._wake = asyncio.Event(), asyncio.Event()
        self._active: asyncio.Task[None] | None = None

    async def run(self) -> None:
        layout = ProjectLayout.from_lean_project(self._project)
        layout.validate_runtime()
        session_id = self._trusted_id(self._dependencies.session_ids, _SESSION_ID)
        ownership = acquire_state_process(
            layout.run_root / "state.pid",
            layout.run_root / "state.sock",
        )
        state: StateService | None = None
        started = False
        stop_reason = "CONTROLLER_FAILED"
        try:
            state = StateService(
                StateServiceConfig(layout.root, session_id),
                StateDependencies(control_committed=lambda _operation: self._wake.set()),
            )
            await state.start()
            await DocumentBroker(layout.root, state, smoke_root=layout.root).recover()
            recover_unclean_controller(state)
            interrupt_nonterminal_executions(state)
            project_id = self._project_id(state, layout.root)
            config = load_config(layout.root)
            configured = state.query_projection("controller", _CONTROLLER_ID)
            if configured is None:
                raise ControllerExecutionError("CONTROLLER_NOT_CONFIGURED")
            payload = _payload(configured)
            try:
                provider = ControllerProvider(_text(payload, "provider"))
            except ValueError:
                raise ControllerExecutionError("CONTROLLER_PROVIDER_INVALID") from None
            controller_model = payload.get("model")
            if controller_model is not None and type(controller_model) is not str:
                raise ControllerExecutionError("CONTROLLER_MODEL_INVALID")
            controller = self._dependencies.controller_backend(provider, controller_model)
            worker_model = config.model
            if worker_model is None:
                raise ControllerExecutionError("WORKER_MODEL_REQUIRED")
            worker = self._dependencies.worker_backend()
            identity = controller.identity
            if identity.executable_sha256 is None:
                raise ControllerExecutionError("CONTROLLER_BACKEND_INVALID")
            start_controller(
                state,
                session_id=session_id,
                controller_version=configured.version,
                provider=provider,
                backend_version=identity.version,
                executable_hash=identity.executable_sha256,
            )
            started = True
            governor = ResourceGovernor(
                config.resources,
                disk_free=lambda path: shutil.disk_usage(path).free,
            )
            try:
                await controller.preflight()
                await self._dependencies.worker_preflight()
                governor.validate(layout.root, config.run.lean_runtime)
            except Exception:  # noqa: BROAD_EXCEPT_OK - readiness boundary
                stop_reason = "PREFLIGHT_FAILED"
                raise
            dispatcher = ControllerDispatcher(
                ControllerDispatcherDependencies(
                    state,
                    layout.root,
                    worker_model,
                    governor,
                    worker,
                    controller,
                    self._dependencies.directive_ids,
                )
            )
            await self._loop(
                state,
                ControllerRun(configured.version, session_id, project_id),
                dispatcher,
            )
            stop_reason = "OPERATOR_SIGNAL"
        except asyncio.CancelledError:
            stop_reason = "OPERATOR_SIGNAL"
            raise
        finally:
            try:
                if state is not None:
                    if started:
                        self._record_stop(state, session_id, stop_reason)
                    state.checkpoint()
                    await state.aclose()
            finally:
                ownership.close()

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()
        active = self._active
        if active is not None:
            active.cancel()

    async def _loop(
        self,
        state: StateService,
        run: ControllerRun,
        dispatcher: ControllerDispatcher,
    ) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            claim = claim_next(
                state,
                run,
                self._dependencies.execution_ids,
            )
            if claim is None:
                await self._wake.wait()
                continue
            context = controller_context(state, claim, run)
            active = asyncio.create_task(dispatcher.plan_and_execute(claim, context))
            self._active = active
            try:
                await active
            except asyncio.CancelledError:
                if not self._stop.is_set():
                    raise
            finally:
                self._active = None

    @staticmethod
    def _project_id(state: StateService, project: Path) -> str:
        records = tuple(
            record for record in state.projections("project") if "project_id" in _payload(record)
        )
        if len(records) > 1:
            raise ControllerExecutionError("PROJECT_STATE_INVALID")
        if records:
            return _text(_payload(records[0]), "project_id")
        state.append_event(
            AppendEventCommand(
                "ProjectInitialized",
                "supervisor",
                None,
                None,
                {
                    "project_id": project.name,
                    "base_epoch": smoke_base_epoch(project),
                    "knowledge_epoch": 0,
                },
            )
        )
        return project.name

    @staticmethod
    def _trusted_id(factory: Callable[[], str], pattern: re.Pattern[str]) -> str:
        value = factory()
        if type(value) is not str or pattern.fullmatch(value) is None:
            raise ControllerExecutionError("TRUSTED_ID_INVALID")
        return value

    @staticmethod
    def _record_stop(state: StateService, session_id: str, reason_code: str) -> None:
        payload: dict[str, JsonValue] = {
            "controller_id": _CONTROLLER_ID,
            "controller_session_id": session_id,
            "reason_code": reason_code,
        }
        state.append_event(
            AppendEventCommand("ControllerStopped", _CONTROLLER_ID, None, None, payload)
        )


def _payload(record: ProjectionRecord) -> dict[str, JsonValue]:
    document: JsonValue = json.loads(record.state_json)
    payload = document.get("payload") if type(document) is dict else None
    if type(payload) is not dict:
        raise ControllerExecutionError("CONTROLLER_STATE_INVALID")
    return payload


def _text(payload: dict[str, JsonValue], field: str) -> str:
    value = payload.get(field)
    if type(value) is not str or not value:
        raise ControllerExecutionError("CONTROLLER_STATE_INVALID")
    return value


def _unavailable_controller(
    _provider: ControllerProvider,
    _model: str | None,
) -> ControllerBackend:
    raise ControllerExecutionError("CONTROLLER_BACKEND_UNAVAILABLE")


def _default_dependencies() -> ControllerSupervisorDependencies:
    return ControllerSupervisorDependencies(
        controller_backend=_unavailable_controller,
        worker_backend=create_codex_backend,
        worker_preflight=lambda: asyncio.sleep(0),
        session_ids=lambda: f"controller-{secrets.token_hex(16)}",
        execution_ids=lambda: f"execution-{secrets.token_hex(16)}",
        directive_ids=lambda: f"directive-{secrets.token_hex(16)}",
    )
