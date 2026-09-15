from __future__ import annotations

import asyncio  # noqa: ANYIO_OK - existing broker/consumer lifecycle
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from aizim.gateway import (
    CapabilityDependencies,
    CapabilityGateway,
    CapabilityIssuer,
    GatewayLimits,
    GatewaySessionBroker,
    current_process_image_sha256,
)
from aizim.gateway.socket_alias import ProjectSocketAlias
from aizim.knowledge import ArtifactStore, RuntimePromotionVerifier
from aizim.lean import DocumentBroker, SharedLeanRuntime
from aizim.state import StateService
from aizim.state.events import utc_now

from .evaluation_contract import environment_fingerprint
from .knowledge_stream import KnowledgeStream
from .promotion_consumer import PromotionConsumer
from .resources import ResourceGovernor
from .worker import WorkerRunner
from .worker_authority import BrokerWorkerAuthority, WorkerBackend, WorkerDirective
from .worker_cursor import WorkerCursor
from .worker_gateway import WorkerGatewayActions


class _Consumer(Protocol):
    def notify_submission(self) -> None: ...

    async def run(self, stop: asyncio.Event) -> None: ...

    async def wait_for_failure(self) -> None: ...

    async def wait_for_epoch(self, epoch: int) -> None: ...


class _SocketAlias(Protocol):
    socket_path: Path

    def close(self) -> None: ...


class WorkerHostError(RuntimeError):
    pass


type _ConsumerFactory = Callable[
    [StateService, ArtifactStore, RuntimePromotionVerifier, KnowledgeStream],
    _Consumer,
]


@dataclass(frozen=True, slots=True)
class _HostFactories:
    alias: Callable[[Path], _SocketAlias]
    consumer: _ConsumerFactory


@dataclass(frozen=True, slots=True)
class _HostResources:
    broker: DocumentBroker
    runtime: SharedLeanRuntime
    consumer: _Consumer
    sessions: GatewaySessionBroker
    alias: _SocketAlias
    runner: WorkerRunner
    run_id: str


async def _close_resources(
    sessions: GatewaySessionBroker | None,
    runtime: SharedLeanRuntime,
    alias: _SocketAlias,
) -> tuple[BaseException, ...]:
    errors: list[BaseException] = []
    operations = (runtime.aclose,) if sessions is None else (sessions.aclose, runtime.aclose)
    for operation in operations:
        try:
            await operation()
        except BaseException as error:  # noqa: BROAD_EXCEPT_OK - cleanup
            errors.append(error)
    try:
        alias.close()
    except BaseException as error:  # noqa: BROAD_EXCEPT_OK - cleanup
        errors.append(error)
    return tuple(errors)


async def _settle_cleanup(
    task: asyncio.Task[tuple[BaseException, ...]], primary: BaseException | None
) -> None:
    interruption: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError as error:
            interruption = interruption or error
    errors = task.result()
    if primary is not None:
        for error in errors:
            primary.add_note(f"cleanup failure: {type(error).__name__}: {error}")
        return
    if errors:
        raise errors[0] from interruption
    if interruption is not None:
        raise interruption


class WorkerExecutionHost:
    def __init__(self, resources: _HostResources) -> None:
        self._resources = resources
        self._stop = asyncio.Event()
        self._consumer_task: asyncio.Task[None] | None = None
        self._failure_task: asyncio.Task[None] | None = None
        self._active_workers: set[asyncio.Task[WorkerCursor]] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._close_task: asyncio.Task[tuple[BaseException, ...]] | None = None
        self._closed = False

    @classmethod
    async def open(
        cls,
        state: StateService,
        project_root: Path,
        smoke_root: Path,
        governor: ResourceGovernor,
        run_id: str,
        *,
        continue_on_failure: bool = False,
    ) -> WorkerExecutionHost:
        return await cls._open_with_factories(
            state,
            project_root,
            smoke_root,
            governor,
            run_id,
            _HostFactories(
                ProjectSocketAlias,
                lambda state, artifacts, verifier, knowledge: PromotionConsumer(
                    state, artifacts, verifier, knowledge, continue_on_failure=continue_on_failure
                ),
            ),
        )

    @classmethod
    async def _open_with_factories(
        cls,
        state: StateService,
        project_root: Path,
        smoke_root: Path,
        governor: ResourceGovernor,
        run_id: str,
        factories: _HostFactories,
    ) -> WorkerExecutionHost:
        broker = DocumentBroker(project_root, state, smoke_root=smoke_root)
        await broker.recover()
        runtime = SharedLeanRuntime(state, broker, run_id)
        knowledge = KnowledgeStream(state)
        artifacts = ArtifactStore(project_root)
        consumer = factories.consumer(
            state, artifacts, RuntimePromotionVerifier(runtime, broker, run_id), knowledge
        )
        actions = WorkerGatewayActions(
            state,
            broker,
            runtime,
            knowledge,
            artifacts,
            environment_fingerprint(smoke_root),
            consumer.notify_submission,
        )
        gateway = CapabilityGateway(
            state,
            actions.targets(),
            GatewayLimits(200, 60.0, 200),
            CapabilityDependencies(utc_now, time.monotonic, lambda: secrets.token_hex(16)),
        )
        client_image_hash = current_process_image_sha256()
        alias = factories.alias(project_root)
        sessions: GatewaySessionBroker | None = None
        host: WorkerExecutionHost | None = None
        try:
            sessions = GatewaySessionBroker(alias.socket_path, gateway=gateway)
            authority = BrokerWorkerAuthority(CapabilityIssuer(state), sessions, client_image_hash)
            runner = WorkerRunner(state, broker, governor, authority, project_root, run_id)
            host = cls(_HostResources(broker, runtime, consumer, sessions, alias, runner, run_id))
            await sessions.start()
            host._consumer_task = asyncio.create_task(consumer.run(host._stop))
            host._failure_task = asyncio.create_task(consumer.wait_for_failure())
        except BaseException as primary:  # noqa: BROAD_EXCEPT_OK - rollback
            cleanup = asyncio.create_task(
                host._cleanup() if host is not None else _close_resources(sessions, runtime, alias)
            )
            await _settle_cleanup(cleanup, primary)
            raise
        return host

    async def run(self, directive: WorkerDirective, backend: WorkerBackend) -> WorkerCursor:
        async with self._lifecycle_lock:
            if self._closed:
                raise WorkerHostError("WORKER_HOST_CLOSED")
            worker = asyncio.create_task(self._resources.runner.run(directive, backend))
            self._active_workers.add(worker)
        try:
            return await self._race_failure(worker)
        finally:
            async with self._lifecycle_lock:
                self._active_workers.discard(worker)

    async def prewarm(self) -> None:
        async def prepare() -> None:
            project = await self._resources.broker._trusted_promotion_project(
                self._resources.run_id
            )
            await self._resources.runtime.prepare(project)

        await self._race_failure(asyncio.create_task(prepare()))

    async def _race_failure[T](self, task: asyncio.Task[T]) -> T:
        failure = self._failure_task
        if failure is None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise WorkerHostError("WORKER_HOST_NOT_STARTED")
        try:
            completed, _ = await asyncio.wait((task, failure), return_when=asyncio.FIRST_COMPLETED)
            if failure in completed:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await failure
            return await task
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def wait_for_epoch(self, epoch: int, timeout_seconds: float) -> None:
        await asyncio.wait_for(self._resources.consumer.wait_for_epoch(epoch), timeout_seconds)

    async def aclose(self) -> None:
        primary = sys.exception()
        async with self._lifecycle_lock:
            task = self._close_task
            if task is None:
                self._closed = True
                task = asyncio.create_task(self._cleanup())
                self._close_task = task
        await _settle_cleanup(task, primary)

    async def _cleanup(self) -> tuple[BaseException, ...]:
        resources = self._resources
        async with self._lifecycle_lock:
            workers = tuple(self._active_workers)
            for worker in workers:
                worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        failure = self._failure_task
        if failure is not None:
            failure.cancel()
            await asyncio.gather(failure, return_exceptions=True)
        self._stop.set()
        errors: list[BaseException] = []
        try:
            resources.consumer.notify_submission()
        except BaseException as error:  # noqa: BROAD_EXCEPT_OK - cleanup
            errors.append(error)
        await asyncio.sleep(0)
        consumer = self._consumer_task
        if consumer is not None:
            if not consumer.done():
                consumer.cancel()
            result = (await asyncio.gather(consumer, return_exceptions=True))[0]
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                errors.append(result)
        errors.extend(
            await _close_resources(resources.sessions, resources.runtime, resources.alias)
        )
        return tuple(errors)
