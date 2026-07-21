from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import TracebackType

from aizim.domain.serialization import JsonValue

from .events import (
    EVENT_SCHEMA_VERSION,
    EventEnvelope,
    EventValidationError,
    MonotoneUlidFactory,
    event_as_dict,
    utc_now,
)
from .projections import ProjectionRecord, ProjectionReducer, apply_event
from .rpc import (
    Dispatch,
    RpcProtocolError,
    RpcRequest,
    RpcResponse,
    RpcServer,
    RpcSuccess,
    rpc_failure,
    start_rpc_server,
)
from .store import (
    DuplicateEventError,
    EventRecord,
    ProjectionAuthorityError,
    ReplayVerification,
    StoreHealth,
    _EventStore,
)


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


@dataclass(frozen=True, slots=True)
class AppendEventCommand:
    event_type: str
    actor: str
    run_id: str | None
    causation_id: str | None
    payload: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class StateServiceLifecycleError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return self.reason


def _text(params: dict[str, JsonValue], key: str) -> str:
    value = params.get(key)
    if type(value) is not str or not value:
        raise RpcProtocolError("INVALID_PARAMS")
    return value


def _optional_text(params: dict[str, JsonValue], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if type(value) is not str or not value:
        raise RpcProtocolError("INVALID_PARAMS")
    return value


def _keys(
    params: dict[str, JsonValue], required: frozenset[str], optional: frozenset[str] = frozenset()
) -> None:
    if not required <= params.keys() or params.keys() - required - optional:
        raise RpcProtocolError("INVALID_PARAMS")


def _append_command(params: dict[str, JsonValue]) -> AppendEventCommand:
    _keys(
        params,
        frozenset({"event_type", "actor", "run_id", "causation_id", "payload"}),
    )
    payload = params["payload"]
    if type(payload) is not dict:
        raise RpcProtocolError("INVALID_PARAMS")
    return AppendEventCommand(
        event_type=_text(params, "event_type"),
        actor=_text(params, "actor"),
        run_id=_optional_text(params, "run_id"),
        causation_id=_optional_text(params, "causation_id"),
        payload=payload,
    )


def _projection_result(record: ProjectionRecord | None) -> JsonValue:
    if record is None:
        return None
    state: JsonValue = json.loads(record.state_json)
    return {
        "entity_id": record.entity_id,
        "projection_name": record.projection_name,
        "state": state,
        "version": record.version,
    }


class StateService:
    def __init__(
        self, config: StateServiceConfig, dependencies: StateDependencies | None = None
    ) -> None:
        self._config = config
        self._dependencies = StateDependencies() if dependencies is None else dependencies
        self._store = _EventStore(
            config.project_root / ".aizim" / "state.sqlite3",
            self._dependencies.reducer,
        )
        self._rpc: RpcServer | None = None
        self._closed = False

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
        self._rpc = await start_rpc_server(
            self.socket_path, self._config.service_session, dispatch
        )

    def close(self) -> None:
        if self._rpc is not None:
            raise StateServiceLifecycleError("running RPC service requires asynchronous close")
        if not self._closed:
            self._store.close()
            self._closed = True

    async def aclose(self) -> None:
        if self._rpc is not None:
            await self._rpc.close()
            self._rpc = None
        if not self._closed:
            self._store.close()
            self._closed = True

    def append_event(self, command: AppendEventCommand) -> EventRecord:
        occurred_at = self._dependencies.clock()
        event = EventEnvelope(
            event_id=self._dependencies.event_ids(),
            schema_version=EVENT_SCHEMA_VERSION,
            event_type=command.event_type,
            occurred_at=occurred_at,
            actor=command.actor,
            run_id=command.run_id,
            causation_id=command.causation_id,
            payload=command.payload,
        )
        return self._store.append(event)

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
        handlers: dict[str, Callable[[RpcRequest, bool], RpcResponse]] = {
            "health": self._rpc_health,
            "append_event": self._rpc_append,
            "query_projection": self._rpc_projection,
            "query_events": self._rpc_events,
            "logical_digest": self._rpc_digest,
            "replay_verify": self._rpc_replay,
        }
        handler = handlers.get(request.operation)
        if handler is None:
            return rpc_failure("UNKNOWN_OPERATION", "operation is not available")
        try:
            return handler(request, trusted)
        except RpcProtocolError:
            return rpc_failure("INVALID_PARAMS", "request parameters are invalid")
        except EventValidationError:
            return rpc_failure("INVALID_EVENT", "event request is invalid")
        except DuplicateEventError as error:
            return rpc_failure("DUPLICATE_EVENT", "event id already exists", error.event_id)
        except ProjectionAuthorityError:
            return rpc_failure("INVALID_PROJECTION", "projection is not available")

    def _rpc_health(self, request: RpcRequest, trusted: bool) -> RpcResponse:
        del trusted
        _keys(request.params, frozenset())
        return RpcSuccess(
            {"event_schema_version": self.health().event_schema_version, "ready": True}
        )

    def _rpc_append(self, request: RpcRequest, trusted: bool) -> RpcResponse:
        if not trusted:
            return rpc_failure("NOT_AUTHORIZED", "trusted service session is required")
        record = self.append_event(_append_command(request.params))
        return RpcSuccess(
            {"event_id": record.envelope.event_id, "sequence": record.sequence}
        )

    def _rpc_projection(self, request: RpcRequest, trusted: bool) -> RpcResponse:
        del trusted
        _keys(request.params, frozenset({"projection_name", "entity_id"}))
        return RpcSuccess(
            _projection_result(
                self.query_projection(
                    _text(request.params, "projection_name"),
                    _text(request.params, "entity_id"),
                )
            )
        )

    def _rpc_events(self, request: RpcRequest, trusted: bool) -> RpcResponse:
        del trusted
        _keys(request.params, frozenset(), frozenset({"run_id"}))
        records = self.query_events(_optional_text(request.params, "run_id"))
        return RpcSuccess(
            [{"sequence": record.sequence, **event_as_dict(record.envelope)} for record in records]
        )

    def _rpc_digest(self, request: RpcRequest, trusted: bool) -> RpcResponse:
        del trusted
        _keys(request.params, frozenset())
        return RpcSuccess({"logical_digest": self.logical_digest()})

    def _rpc_replay(self, request: RpcRequest, trusted: bool) -> RpcResponse:
        del trusted
        _keys(request.params, frozenset())
        result = self.replay_verify()
        return RpcSuccess(
            {"logical_digest": result.logical_digest, "matched": result.matched}
        )
