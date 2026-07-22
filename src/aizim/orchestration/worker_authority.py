from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from aizim.agents import AgentRequest, AgentResult
from aizim.domain import FileLease
from aizim.gateway import (
    BrokerRegistration,
    CapabilityGrant,
    CapabilityIssuer,
    GatewaySessionBroker,
    advertised_tools,
)

if TYPE_CHECKING:
    from .worker import WorkerDirective


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
