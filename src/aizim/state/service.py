from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import TracebackType

from aizim.domain.serialization import JsonValue

from .capabilities import CapabilityRecord, canonical_timestamp
from .event_payload import thaw_payload
from .events import (
    EVENT_SCHEMA_VERSION,
    EventEnvelope,
    EventValidationError,
    MonotoneUlidFactory,
    utc_now,
)
from .operations import AppendEventCommand, dispatch_operation
from .projections import ProjectionRecord, ProjectionReducer, apply_event
from .rpc import (
    Dispatch,
    RpcRequest,
    RpcResponse,
    RpcServer,
    start_rpc_server,
)
from .service_ownership import (
    StateOwnership,
    StateOwnershipError,
    acquire_state_ownership,
)
from .store import (
    EventRecord,
    ReplayVerification,
    StoreHealth,
    _EventStore,
)
from .store_contracts import InitializationCheckpoint, continue_initialization


@dataclass(frozen=True, slots=True)
class StateServiceConfig:
    project_root: Path
    service_session: str

    def __post_init__(self) -> None:
        if not isinstance(self.project_root, Path):
            raise EventValidationError("project_root", "must be a path")
        if type(self.service_session) is not str or not self.service_session:
            raise EventValidationError("service_session", "must be a non-empty string")


@dataclass(frozen=True, slots=True)
class StateDependencies:
    clock: Callable[[], datetime] = utc_now
    event_ids: Callable[[], str] = field(default_factory=MonotoneUlidFactory)
    reducer: ProjectionReducer = apply_event
    before_initialization_commit: InitializationCheckpoint = continue_initialization


@dataclass(frozen=True, slots=True)
class StateServiceLifecycleError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return self.reason


class StateService:
    def __init__(
        self, config: StateServiceConfig, dependencies: StateDependencies | None = None
    ) -> None:
        self._config = config
        self._dependencies = StateDependencies() if dependencies is None else dependencies
        self._rpc: RpcServer | None = None
        self._closed = False
        try:
            ownership = acquire_state_ownership(
                config.project_root / ".aizim" / "run" / "state.lock"
            )
        except StateOwnershipError as error:
            raise StateServiceLifecycleError(error.reason) from error
        store_opened = False
        try:
            store = _EventStore(
                config.project_root / ".aizim" / "state.sqlite3",
                self._dependencies.reducer,
                self._dependencies.before_initialization_commit,
            )
            store_opened = True
        finally:
            if not store_opened:
                ownership.close()
        self._ownership: StateOwnership = ownership
        self._store = store

    @property
    def socket_path(self) -> Path:
        return self._config.project_root / ".aizim" / "run" / "state.sock"

    def __enter__(self) -> StateService:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        self.close()

    async def __aenter__(self) -> StateService:
        await self.start()
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        await self.aclose()

    async def start(self) -> None:
        if self._closed or self._rpc is not None:
            raise StateServiceLifecycleError("state service cannot be started in its current state")
        dispatch: Dispatch = self._dispatch
        started = False
        try:
            self._rpc = await start_rpc_server(
                self.socket_path, self._config.service_session, dispatch
            )
            started = True
        finally:
            if not started:
                self.close()

    def close(self) -> None:
        if self._rpc is not None:
            raise StateServiceLifecycleError("running RPC service requires asynchronous close")
        if not self._closed:
            self._closed = True
            try:
                self._store.close()
            finally:
                self._ownership.close()

    async def aclose(self) -> None:
        rpc = self._rpc
        self._rpc = None
        try:
            if rpc is not None:
                await rpc.close()
        finally:
            self.close()

    def append_event(self, command: AppendEventCommand) -> EventRecord:
        return self._store.append(self._event(command))

    def persist_capability(self, capability: CapabilityRecord) -> EventRecord:
        payload: dict[str, JsonValue] = {
            "worker_id": capability.worker_id,
            "role": capability.role,
            "operations": list(capability.operations),
            "expires_at": canonical_timestamp(capability.expires_at),
        }
        if capability.lease_id is not None:
            payload["lease_id"] = capability.lease_id
        event = self._event(
            AppendEventCommand(
                event_type="CapabilityMinted",
                actor="capability_issuer",
                run_id=capability.run_id,
                causation_id=None,
                payload=payload,
            )
        )
        return self._store.persist_capability(capability, event)

    def capability_record(self, token_hash: str) -> CapabilityRecord | None:
        return self._store.capability_record(token_hash)

    def revoke_capability(self, token_hash: str) -> bool:
        return self._store.revoke_capability(token_hash, self._dependencies.clock())

    def _event(self, command: AppendEventCommand) -> EventEnvelope:
        return EventEnvelope(
            event_id=self._dependencies.event_ids(),
            schema_version=EVENT_SCHEMA_VERSION,
            event_type=command.event_type,
            occurred_at=self._dependencies.clock(),
            actor=command.actor,
            run_id=command.run_id,
            causation_id=command.causation_id,
            payload=thaw_payload(command.payload),
        )

    def health(self) -> StoreHealth:
        return self._store.health()

    def query_projection(self, name: str, entity_id: str) -> ProjectionRecord | None:
        return self._store.query_projection(name, entity_id)

    def query_events(self, run_id: str | None = None) -> tuple[EventRecord, ...]:
        return self._store.query_events(run_id)

    def canonical_projection_json(self) -> bytes:
        return self._store.canonical_projection_json()

    def logical_digest(self) -> str:
        return self._store.logical_digest()

    def replay_verify(self) -> ReplayVerification:
        return self._store.replay_verify()

    def _dispatch(self, request: RpcRequest, trusted: bool) -> RpcResponse:
        return dispatch_operation(self, request, trusted)
