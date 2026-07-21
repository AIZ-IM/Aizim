from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from hmac import compare_digest
from typing import cast

from aizim.domain import AgentRole
from aizim.domain.serialization import JsonValue
from aizim.state.capabilities import CapabilityRecord
from aizim.state.operations import AppendEventCommand
from aizim.state.service import StateService

from .capabilities import (
    AuthorizedCall,
    CapabilitySession,
    GatewayError,
    GatewayFailure,
    GatewaySuccess,
    GatewayTool,
    advertised_tools,
)

type GatewayTarget = Callable[[AuthorizedCall], JsonValue | Awaitable[JsonValue]]
type GatewayResult = GatewaySuccess | GatewayFailure
_PUBLIC_DENIAL = "operation is not allowed for this worker"
_TOKEN_SHAPED = re.compile(r"[A-Za-z0-9_-]{32,}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_REDACTED = "<redacted>"


@dataclass(frozen=True, slots=True)
class GatewayLimits:
    request_limit: int
    window_seconds: float
    run_budget: int

    def __post_init__(self) -> None:
        if type(self.request_limit) is not int or self.request_limit <= 0:
            raise ValueError("request_limit must be a positive integer")
        if not math.isfinite(self.window_seconds) or self.window_seconds <= 0:
            raise ValueError("window_seconds must be finite and positive")
        if type(self.run_budget) is not int or self.run_budget <= 0:
            raise ValueError("run_budget must be a positive integer")


@dataclass(frozen=True, slots=True)
class CapabilityDependencies:
    clock: Callable[[], datetime]
    monotonic: Callable[[], float]
    request_ids: Callable[[], str]


def _safe_identifier(value: str | None) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        return _REDACTED
    if _TOKEN_SHAPED.search(value) is not None:
        return _REDACTED
    return value


def _safe_role(value: str | None) -> str:
    try:
        return AgentRole(value).value
    except (TypeError, ValueError):
        return _REDACTED


def _safe_operation(value: GatewayTool | str) -> str:
    try:
        return GatewayTool(value).value
    except (TypeError, ValueError):
        return _REDACTED


class CapabilityGateway:
    def __init__(
        self,
        state: StateService,
        targets: Mapping[GatewayTool, GatewayTarget],
        limits: GatewayLimits,
        dependencies: CapabilityDependencies,
    ) -> None:
        self._state = state
        self._targets = dict(targets)
        self._limits = limits
        self._dependencies = dependencies
        self._window_started = dependencies.monotonic()
        self._window_count = 0
        self._run_counts: dict[str, int] = {}

    def discover(self, session: CapabilitySession) -> tuple[GatewayTool, ...]:
        return advertised_tools(session.role)

    def _take_global_request(self) -> bool:
        now = self._dependencies.monotonic()
        if not math.isfinite(now):
            return False
        if now < self._window_started or now - self._window_started >= self._limits.window_seconds:
            self._window_started = now
            self._window_count = 0
        if self._window_count >= self._limits.request_limit:
            return False
        self._window_count += 1
        return True

    def _take_run_request(self, run_id: str) -> bool:
        count = self._run_counts.get(run_id, 0)
        if count >= self._limits.run_budget:
            return False
        self._run_counts[run_id] = count + 1
        return True

    def _deny(
        self,
        reason: str,
        request_id: str,
        session: CapabilitySession,
        operation: GatewayTool | str,
        persisted_run: str | None = None,
    ) -> GatewayFailure:
        event = self._state.append_event(
            AppendEventCommand(
                event_type="CapabilityDenied",
                actor="capability_gateway",
                run_id=_safe_identifier(
                    persisted_run if persisted_run is not None else session.run_id
                ),
                causation_id=None,
                payload={
                    "reason_code": reason,
                    "role": _safe_role(session.role),
                    "worker_id": _safe_identifier(session.worker_id),
                    "operation": _safe_operation(operation),
                    "request_id": _safe_identifier(request_id),
                },
            )
        )
        return GatewayFailure(
            GatewayError("CAPABILITY_DENIED", _PUBLIC_DENIAL, event.envelope.event_id)
        )

    async def call(
        self,
        session: CapabilitySession,
        operation: GatewayTool | str,
        payload: dict[str, JsonValue],
    ) -> GatewayResult:
        request_id = self._dependencies.request_ids()
        if not self._take_global_request():
            return self._deny("REQUEST_RATE_EXCEEDED", request_id, session, operation)
        if type(session.token) is not str or not session.token:
            return self._deny("MISSING_TOKEN", request_id, session, operation)
        presented_hash = sha256(session.token.encode()).hexdigest()
        record = self._state.capability_record(presented_hash)
        stored_hash = "0" * 64 if record is None else record.token_hash
        digest_matches = compare_digest(stored_hash, presented_hash)
        if record is None or not digest_matches:
            return self._deny("UNKNOWN_TOKEN", request_id, session, operation)
        if not self._take_run_request(record.run_id):
            return self._deny("RUN_BUDGET_EXHAUSTED", request_id, session, operation, record.run_id)
        now = self._dependencies.clock()
        if now >= record.expires_at:
            return self._deny("TOKEN_EXPIRED", request_id, session, operation, record.run_id)
        if record.revoked_at is not None:
            return self._deny("TOKEN_REVOKED", request_id, session, operation, record.run_id)
        reason = self._binding_denial(record, session)
        if reason is not None:
            return self._deny(reason, request_id, session, operation, record.run_id)
        try:
            parsed_role = AgentRole(record.role)
            parsed_operation = (
                operation if isinstance(operation, GatewayTool) else GatewayTool(operation)
            )
        except ValueError:
            return self._deny(
                "UNADVERTISED_OPERATION", request_id, session, operation, record.run_id
            )
        if parsed_operation not in advertised_tools(parsed_role):
            return self._deny(
                "UNADVERTISED_OPERATION", request_id, session, operation, record.run_id
            )
        if parsed_operation.value not in record.operations:
            return self._deny(
                "OPERATION_NOT_MINTED", request_id, session, operation, record.run_id
            )
        target = self._targets.get(parsed_operation)
        if target is None:
            return self._deny("TARGET_UNREGISTERED", request_id, session, operation, record.run_id)
        result = target(
            AuthorizedCall(
                request_id=request_id,
                run_id=record.run_id,
                worker_id=record.worker_id,
                role=parsed_role,
                lease_id=record.lease_id,
                operation=parsed_operation,
                payload=payload,
            )
        )
        if isinstance(result, Awaitable):
            result = await cast(Awaitable[JsonValue], result)
        return GatewaySuccess(result)

    @staticmethod
    def _binding_denial(record: CapabilityRecord, session: CapabilitySession) -> str | None:
        if session.run_id != record.run_id:
            return "RUN_MISMATCH"
        if session.worker_id != record.worker_id:
            return "WORKER_MISMATCH"
        if session.role != record.role:
            return "ROLE_MISMATCH"
        if session.lease_id != record.lease_id:
            return "LEASE_MISMATCH"
        return None
