from __future__ import annotations

import asyncio  # noqa: ANYIO_OK - composes existing asyncio RPC and worker lifecycles
import re
import secrets
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from aizim.agents import AgentBackend
from aizim.async_lifecycle import await_cleanup
from aizim.config import load_config
from aizim.lean import DocumentBroker
from aizim.lean.broker_knowledge import current_epoch
from aizim.lean.models import DocumentBrokerError
from aizim.orchestration.codex_worker import create_codex_backend, preflight_codex_worker
from aizim.runtime.layout import ProjectLayout
from aizim.runtime.state_process import StateProcessOwnership, acquire_state_process
from aizim.state import StateDependencies, StateService, StateServiceConfig

from .codex_controller import production_codex
from .control_plane import ControllerProvider
from .controller_backend import ControllerBackend
from .controller_dispatcher import (
    ControllerDispatcher,
    ControllerDispatcherDependencies,
    ControllerRun,
    prepare_next,
    record_controller_stop,
    unavailable_worker_preflight,
)
from .controller_execution import (
    ControllerExecutionError,
    interrupt_nonterminal_executions,
)
from .controller_lifecycle import (
    _payload,
    _text,
    recover_unclean_controller,
    start_controller,
)
from .resources import ResourceGovernor


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
        self._dependencies = dependencies or _default_dependencies(project)
        self._stop, self._wake = asyncio.Event(), asyncio.Event()
        self._idle = asyncio.Event()
        self._active: asyncio.Task[None] | None = None

    async def run(self) -> None:
        layout = ProjectLayout.from_lean_project(self._project)
        layout.validate_runtime()
        session_id = self._trusted_id(self._dependencies.session_ids, "controller")
        ownership = acquire_state_process(
            layout.run_root / "state.pid",
            layout.run_root / "state.sock",
        )
        state: StateService | None = None
        started = False
        stop_reason = "CONTROLLER_FAILED"
        primary: BaseException | None = None
        try:
            state = StateService(
                StateServiceConfig(layout.root, session_id),
                StateDependencies(control_committed=lambda _operation: self._wake.set()),
            )
            await state.start()
            await DocumentBroker(layout.root, state, smoke_root=layout.root).recover()
            recover_unclean_controller(state)
            interrupt_nonterminal_executions(state)
            config = load_config(layout.root)
            configured = state.query_projection("controller", "primary")
            if configured is None:
                raise ControllerExecutionError("CONTROLLER_NOT_CONFIGURED")
            try:
                provider = ControllerProvider(_text(_payload(configured), "provider"))
            except ValueError:
                raise ControllerExecutionError("CONTROLLER_PROVIDER_INVALID") from None
            controller_model = _payload(configured).get("model")
            if controller_model is not None and type(controller_model) is not str:
                raise ControllerExecutionError("CONTROLLER_MODEL_INVALID")
            controller = self._dependencies.controller_backend(provider, controller_model)
            initial_run = self._snapshot(state, configured.version, session_id)
            if config.model is None:
                raise ControllerExecutionError("WORKER_MODEL_REQUIRED")
            if controller.identity.executable_sha256 is None:
                raise ControllerExecutionError("CONTROLLER_BACKEND_INVALID")
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
            start_controller(
                state,
                session_id=session_id,
                controller_version=configured.version,
                provider=provider,
                backend_version=controller.identity.version,
                executable_hash=controller.identity.executable_sha256,
            )
            started = True
            dispatcher = ControllerDispatcher(
                ControllerDispatcherDependencies(
                    state,
                    layout.root,
                    config.model,
                    governor,
                    self._dependencies.worker_backend(),
                    controller,
                )
            )
            await self._loop(state, initial_run, dispatcher)
            stop_reason = "OPERATOR_SIGNAL"
        except BaseException as error:  # noqa: BROAD_EXCEPT_OK - primary failure boundary
            primary = error
            if isinstance(error, asyncio.CancelledError):
                stop_reason = "OPERATOR_SIGNAL"
            raise
        finally:
            cleanup = asyncio.create_task(
                _finalize(state, ownership, (started, session_id, stop_reason))
            )
            try:
                interruption = await await_cleanup(cleanup)
            except BaseException:  # noqa: BROAD_EXCEPT_OK - cleanup boundary
                if primary is None:
                    raise
            else:
                if primary is None and interruption is not None:
                    raise interruption

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if (active := self._active) is not None:
            active.cancel()

    async def _loop(
        self,
        state: StateService,
        initial: ControllerRun,
        dispatcher: ControllerDispatcher,
    ) -> None:
        identity = initial.version, initial.session_id
        while not self._stop.is_set():
            self._wake.clear()
            run = self._snapshot(state, *identity)
            ids = (
                self._trusted_id(self._dependencies.execution_ids, "execution"),
                self._trusted_id(self._dependencies.directive_ids, "directive"),
            )
            prepared = prepare_next(state, run, ids)
            if prepared is None:
                self._idle.set()
                try:
                    await self._wake.wait()
                finally:
                    self._idle.clear()
                continue
            entered = asyncio.Event()
            active = asyncio.create_task(dispatcher.plan_and_execute(prepared, entered))
            await entered.wait()
            self._active = active
            if self._stop.is_set():
                active.cancel()
            try:
                await active
            except asyncio.CancelledError:
                if not self._stop.is_set():
                    raise
            finally:
                self._active = None

    @staticmethod
    def _snapshot(state: StateService, version: int, session_id: str) -> ControllerRun:
        identities = tuple(
            _payload(record)
            for record in state.projections("project")
            if "project_id" in _payload(record)
        )
        if not identities:
            raise ControllerExecutionError("PROJECT_IDENTITY_UNAVAILABLE")
        if len(identities) != 1:
            raise ControllerExecutionError("PROJECT_IDENTITY_INVALID")
        if re.fullmatch(r"[0-9a-f]{64}", _text(identities[0], "base_epoch")) is None:
            raise ControllerExecutionError("PROJECT_IDENTITY_INVALID")
        try:
            epoch = current_epoch(state)
        except DocumentBrokerError:
            raise ControllerExecutionError("PROJECT_EPOCH_INVALID") from None
        if re.fullmatch(r"[0-9a-f]{64}", epoch.base_epoch) is None:
            raise ControllerExecutionError("PROJECT_EPOCH_INVALID")
        return ControllerRun(
            version,
            session_id,
            _text(identities[0], "project_id"),
            epoch.base_epoch,
            epoch.knowledge_epoch,
        )

    @staticmethod
    def _trusted_id(factory: Callable[[], str], prefix: str) -> str:
        value = factory()
        if type(value) is not str or re.fullmatch(f"{prefix}-[0-9a-f]{{32}}", value) is None:
            raise ControllerExecutionError("TRUSTED_ID_INVALID")
        return value


async def _finalize(
    state: StateService | None,
    ownership: StateProcessOwnership,
    details: tuple[bool, str, str],
) -> None:
    started, session_id, reason_code = details
    try:
        if state is not None:
            try:
                if started:
                    record_controller_stop(state, session_id, reason_code)
            finally:
                try:
                    state.checkpoint()
                finally:
                    await state.aclose()
    finally:
        ownership.close()


def _default_dependencies(project: Path | None = None) -> ControllerSupervisorDependencies:
    return ControllerSupervisorDependencies(
        controller_backend=lambda provider, model: production_codex(project, provider, model),
        worker_backend=create_codex_backend,
        worker_preflight=(
            unavailable_worker_preflight
            if project is None
            else lambda: preflight_codex_worker(project)
        ),
        session_ids=lambda: f"controller-{secrets.token_hex(16)}",
        execution_ids=lambda: f"execution-{secrets.token_hex(16)}",
        directive_ids=lambda: f"directive-{secrets.token_hex(16)}",
    )
