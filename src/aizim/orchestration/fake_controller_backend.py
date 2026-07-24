from __future__ import annotations

from typing import assert_never

from aizim.agents import BackendIdentity
from aizim.domain import sha256_bytes

from .controller_backend import (
    BlockedDecision,
    ControllerContext,
    ControllerDecision,
    DispatchDecision,
    RejectDecision,
    controller_context_bytes,
)


class FakeControllerBackend:
    def __init__(self, decision: ControllerDecision) -> None:
        self._decision = decision
        self.received_context_bytes: list[bytes] = []

    @property
    def identity(self) -> BackendIdentity:
        return BackendIdentity(
            "fake",
            "deterministic-controller-v1",
            sha256_bytes(b"deterministic-controller-v1"),
        )

    async def preflight(self) -> None:
        return None

    async def plan(self, context: ControllerContext) -> ControllerDecision:
        self.received_context_bytes.append(controller_context_bytes(context))
        match self._decision:
            case DispatchDecision() as decision:
                return decision
            case BlockedDecision() as decision:
                return decision
            case RejectDecision() as decision:
                return decision
            case unreachable:
                assert_never(unreachable)
