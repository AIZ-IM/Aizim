from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from aizim.agents import AgentRequest, AgentResult
from aizim.domain import AgentRole, FileLease
from aizim.gateway import (
    BrokerRegistration,
    CapabilityGrant,
    CapabilityIssuer,
    GatewaySessionBroker,
    GatewayTool,
    advertised_tools,
)


@dataclass(frozen=True, slots=True)
class WorkerDirective:
    directive_id: str
    worker_id: str
    role: AgentRole
    initial_source: bytes
    budget: int
    timeout_seconds: float
    operations: tuple[GatewayTool, ...] | None = None

    def __post_init__(self) -> None:
        if (
            not all(type(value) is str and value for value in (self.directive_id, self.worker_id))
            or type(self.role) is not AgentRole
            or type(self.initial_source) is not bytes
            or not self.initial_source
            or type(self.budget) is not int
            or self.budget < 1
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("INVALID_WORKER_DIRECTIVE")
        operations = advertised_tools(self.role) if self.operations is None else self.operations
        if (
            not operations
            or len(operations) != len(set(operations))
            or any(operation not in advertised_tools(self.role) for operation in operations)
        ):
            raise ValueError("INVALID_WORKER_DIRECTIVE")


@dataclass(frozen=True, slots=True)
class GatewaySession:
    broker_socket: Path
    session_id: str


class WorkerAuthority(Protocol):
    def issue(
        self, run_id: str, directive: WorkerDirective, lease: FileLease
    ) -> GatewaySession: ...


class WorkerBackend(Protocol):
    async def run(self, request: AgentRequest) -> AgentResult: ...


class BrokerWorkerAuthority:
    def __init__(
        self,
        issuer: CapabilityIssuer,
        sessions: GatewaySessionBroker,
        client_image_hash: str,
    ) -> None:
        if type(issuer) is not CapabilityIssuer or type(sessions) is not GatewaySessionBroker:
            raise ValueError("INVALID_WORKER_AUTHORITY")
        self._issuer, self._sessions, self._client_image_hash = issuer, sessions, client_image_hash

    def issue(self, run_id: str, directive: WorkerDirective, lease: FileLease) -> GatewaySession:
        raw_token = self._issuer.mint(
            CapabilityGrant(
                run_id,
                directive.worker_id,
                directive.role,
                lease.lease_id,
                advertised_tools(directive.role)
                if directive.operations is None
                else directive.operations,
                lease.expires_at,
            )
        )
        session_id = self._sessions.register(
            BrokerRegistration(
                run_id,
                directive.worker_id,
                directive.role,
                self._client_image_hash,
                lease.expires_at,
                raw_token,
                lease.lease_id,
                directive.operations,
            )
        )
        return GatewaySession(self._sessions.socket_path, session_id)
