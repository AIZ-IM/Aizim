from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aizim.domain import AgentRole

from .capabilities import GatewayTool
from .transport import GatewayCaller, GatewayChannelClaims, GatewayChannelContext


@dataclass(slots=True)
class PendingSession:
    run_id: str
    worker_id: str
    role: AgentRole
    image_hash: str
    expires_at: datetime
    lease_id: str | None
    operations: tuple[GatewayTool, ...]
    secret: bytearray

    def channel(self, gateway: GatewayCaller) -> GatewayChannelContext:
        claims = GatewayChannelClaims(self.run_id, self.worker_id, self.role, self.lease_id)
        return GatewayChannelContext(gateway, claims, self.secret)

    def clear(self) -> None:
        self.secret[:] = b"\0" * len(self.secret)
