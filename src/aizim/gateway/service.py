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
    GATEWAY_RATE_LIMITED,
    MAX_RETRY_AFTER_SECONDS,
    MIN_RETRY_AFTER_SECONDS,
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
_TARGET_FAILURE = "gateway operation failed"
# Target errors carry stable upper-case reason codes such as UNVERIFIED_DOCUMENT_APPLY.
_PUBLIC_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
# Gateway-owned outcomes cannot be claimed by an authorized target.
_GATEWAY_CODES = frozenset(
    {
        "CAPABILITY_DENIED", "GATEWAY_INTERNAL", "GATEWAY_PROTOCOL", "GATEWAY_UNAVAILABLE",
        GATEWAY_RATE_LIMITED,
    }
)
_TOKEN_SHAPED = re.compile(r"[A-Za-z0-9_-]{32,}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_REDACTED = "<redacted>"


@dataclass(frozen=True, slots=True)
class GatewayLimits:
    request_limit: int
    window_seconds: float
    run_budget: int
    # Calls allowed to one worker identity in a run; None leaves only the run budget.
    worker_budget: int | None = None

    def __post_init__(self) -> None:
        if type(self.request_limit) is not int or self.request_limit <= 0:
            raise ValueError("request_limit must be a positive integer")
        if not math.isfinite(self.window_seconds) or self.window_seconds <= 0:
            raise ValueError("window_seconds must be finite and positive")
        if type(self.run_budget) is not int or self.run_budget <= 0:
            raise ValueError("run_budget must be a positive integer")
        if self.worker_budget is not None and (
            type(self.worker_budget) is not int or self.worker_budget <= 0
        ):
            raise ValueError("worker_budget must be a positive integer")


@dataclass(frozen=True, slots=True)
class CapabilityDependencies:
    clock: Callable[[], datetime]
    monotonic: Callable[[], float]
    request_ids: Callable[[], str]


def _safe_identifier(value: str | None, *, trusted: bool = False) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        return _REDACTED
    # A presented value may smuggle a token; the issuer's persisted identifiers cannot.
    if not trusted and _TOKEN_SHAPED.search(value) is not None:
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
        self._worker_counts: dict[tuple[str, str], int] = {}

    def discover(self, session: CapabilitySession) -> tuple[GatewayTool, ...]:
        return advertised_tools(session.role)

    def _take_global_request(self) -> float | None:
        """Consume a request slot, or return a bounded delay until another try."""
        now = self._dependencies.monotonic()
        if not math.isfinite(now):
            return MAX_RETRY_AFTER_SECONDS
        if now < self._window_started or now - self._window_started >= self._limits.window_seconds:
            self._window_started = now
            self._window_count = 0
        if self._window_count >= self._limits.request_limit:
            remaining = self._limits.window_seconds - (now - self._window_started)
            return min(MAX_RETRY_AFTER_SECONDS, max(MIN_RETRY_AFTER_SECONDS, remaining))
        self._window_count += 1
        return None

    def _take_budgeted_request(self, record: CapabilityRecord) -> str | None:
        count = self._run_counts.get(record.run_id, 0)
        if count >= self._limits.run_budget:
            return "RUN_BUDGET_EXHAUSTED"
        worker = (record.run_id, record.worker_id)
        used = self._worker_counts.get(worker, 0)
        if (
            self._limits.worker_budget is not None and used >= self._limits.worker_budget
        ) or (record.call_budget is not None and used >= record.call_budget):
            return "WORKER_BUDGET_EXHAUSTED"
        self._run_counts[record.run_id] = count + 1
        self._worker_counts[worker] = used + 1
        return None

    def _deny(
        self,
        reason: str,
        request_id: str,
        session: CapabilitySession,
        operation: GatewayTool | str,
        persisted: str | None = None,
        *,
        retry_after_seconds: float | None = None,
    ) -> GatewayFailure:
        event = self._state.append_event(
            AppendEventCommand(
                event_type="CapabilityDenied",
                actor="capability_gateway",
                run_id=_safe_identifier(persisted or session.run_id, trusted=bool(persisted)),
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
        if retry_after_seconds is not None:
            return GatewayFailure(GatewayError(
                GATEWAY_RATE_LIMITED,
                "gateway request rate exceeded; retry after the indicated delay",
                event.envelope.event_id,
                retry_after_seconds,
            ))
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
        retry_after = self._take_global_request()
        if retry_after is not None:
            return self._deny(
                "REQUEST_RATE_EXCEEDED", request_id, session, operation,
                retry_after_seconds=retry_after,
            )
        if type(session.token) is not str or not session.token:
            return self._deny("MISSING_TOKEN", request_id, session, operation)
        presented_hash = sha256(session.token.encode()).hexdigest()
        record = self._state.capability_record(presented_hash)
        stored_hash = "0" * 64 if record is None else record.token_hash
        digest_matches = compare_digest(stored_hash, presented_hash)
        if record is None or not digest_matches:
            return self._deny("UNKNOWN_TOKEN", request_id, session, operation)
        exhausted = self._take_budgeted_request(record)
        if exhausted is not None:
            return self._deny(exhausted, request_id, session, operation, record.run_id)
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
        try:
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
        except Exception as error:  # BROAD_EXCEPT_OK: an authorized target's failure is a result
            return self._target_failure(error, request_id, record, parsed_role, parsed_operation)
        return GatewaySuccess(result)

    def _target_failure(
        self,
        error: Exception,
        request_id: str,
        record: CapabilityRecord,
        role: AgentRole,
        operation: GatewayTool,
    ) -> GatewayFailure:
        """Return a stable reason code to the worker and keep its channel usable."""
        message = error.args[0] if len(error.args) == 1 else None
        code = (
            message
            if type(message) is str
            and _PUBLIC_CODE.fullmatch(message) is not None
            and message not in _GATEWAY_CODES
            else "TOOL_FAILED"
        )
        event = self._state.append_event(
            AppendEventCommand(
                event_type="GatewayCallFailed",
                actor="capability_gateway",
                run_id=_safe_identifier(record.run_id, trusted=True),
                causation_id=None,
                payload={
                    "reason_code": code,
                    "error_type": _safe_identifier(type(error).__name__),
                    "role": role.value,
                    "worker_id": _safe_identifier(record.worker_id, trusted=True),
                    "operation": operation.value,
                    "request_id": _safe_identifier(request_id),
                },
            )
        )
        return GatewayFailure(GatewayError(code, _TARGET_FAILURE, event.envelope.event_id))

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
