from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from aizim.domain import AgentRole
from aizim.gateway import (
    CapabilityDependencies,
    CapabilityGateway,
    CapabilityGrant,
    CapabilitySession,
    GatewayLimits,
    GatewayTool,
)
from aizim.state import StateDependencies, StateService, StateServiceConfig
from aizim.state.projections import apply_event

NOW = datetime(2026, 7, 21, 10, tzinfo=UTC)


class MutableClock:
    def __init__(self) -> None:
        self.wall = NOW
        self.monotonic = 100.0

    def utc_now(self) -> datetime:
        return self.wall

    def monotonic_now(self) -> float:
        return self.monotonic


class Ids:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._next = 0

    def __call__(self) -> str:
        self._next += 1
        return f"{self._next:026d}"


def service(project: Path, clock: MutableClock, *, reducer=apply_event) -> StateService:
    return StateService(
        StateServiceConfig(project, "trusted-state-session"),
        StateDependencies(clock=clock.utc_now, event_ids=Ids("event"), reducer=reducer),
    )


def grant(*, lease_id: str | None = None) -> CapabilityGrant:
    return CapabilityGrant(
        run_id="run-1",
        worker_id="worker-1",
        role=AgentRole.RESEARCH_CONDUCTOR,
        lease_id=lease_id,
        operations=(GatewayTool.STATE_QUERY,),
        expires_at=NOW + timedelta(hours=1),
    )


def session(raw_token: str, *, lease_id: str | None = None) -> CapabilitySession:
    return CapabilitySession(
        token=raw_token,
        run_id="run-1",
        worker_id="worker-1",
        role=AgentRole.RESEARCH_CONDUCTOR.value,
        lease_id=lease_id,
    )


def gateway(
    state: StateService,
    clock: MutableClock,
    target,
    *,
    request_limit: int = 100,
    run_budget: int = 100,
    worker_budget: int | None = None,
) -> CapabilityGateway:
    return CapabilityGateway(
        state=state,
        targets={GatewayTool.STATE_QUERY: target},
        limits=GatewayLimits(request_limit, 60.0, run_budget, worker_budget),
        dependencies=CapabilityDependencies(
            clock=clock.utc_now,
            monotonic=clock.monotonic_now,
            request_ids=Ids("request"),
        ),
    )
