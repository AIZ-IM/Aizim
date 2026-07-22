from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from aizim.domain import AgentRole
from aizim.gateway import (
    AuthorizedCall,
    CapabilityDependencies,
    CapabilityGateway,
    CapabilityGrant,
    CapabilityIssuer,
    CapabilitySession,
    GatewayFailure,
    GatewayLimits,
    GatewaySuccess,
    GatewayTool,
)
from aizim.state import AppendEventCommand, StateDependencies, StateService, StateServiceConfig
from aizim.state.events import EventEnvelope, event_as_dict
from aizim.state.projections import ProjectionRecord, apply_event
from aizim.state.store_contracts import ProjectionAuthorityError

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


class InjectedMutationFailure(RuntimeError):
    pass


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
) -> CapabilityGateway:
    return CapabilityGateway(
        state=state,
        targets={GatewayTool.STATE_QUERY: target},
        limits=GatewayLimits(request_limit, 60.0, run_budget),
        dependencies=CapabilityDependencies(
            clock=clock.utc_now,
            monotonic=clock.monotonic_now,
            request_ids=Ids("request"),
        ),
    )


def test_mint_is_atomic_opaque_and_persistent_across_restart(tmp_path: Path) -> None:
    clock = MutableClock()
    with service(tmp_path, clock) as state:
        raw_token = CapabilityIssuer(state).mint(grant())
        token_hash = sha256(raw_token.encode()).hexdigest()
        record = state.capability_record(token_hash)
        minted = state.query_events("run-1")[-1].envelope

    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", raw_token) is not None
    assert record is not None and record.token_hash == token_hash
    assert record.operations == (GatewayTool.STATE_QUERY.value,)
    serialized = repr(event_as_dict(minted))
    assert raw_token not in serialized
    assert token_hash not in serialized
    assert set(event_as_dict(minted)["payload"]) == {
        "worker_id",
        "role",
        "operations",
        "expires_at",
    }
    with service(tmp_path, clock) as restarted:
        assert restarted.capability_record(token_hash) == record


@pytest.mark.parametrize(
    "invalid_grant",
    [
        lambda: replace(grant(), operations=()),
        lambda: replace(grant(), operations=(GatewayTool.STATE_APPEND,)),
        lambda: replace(
            grant(), operations=(GatewayTool.STATE_QUERY, GatewayTool.STATE_QUERY)
        ),
        lambda: replace(grant(), expires_at=NOW.replace(tzinfo=None)),
    ],
)
def test_grants_reject_escalation_duplicate_operations_and_non_utc_expiry(
    invalid_grant,
) -> None:
    with pytest.raises(ValueError):
        invalid_grant()


def test_duplicate_token_is_atomic_and_revocation_persists_across_restart(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    raw_token = "fixed-opaque-token"
    token_hash = sha256(raw_token.encode()).hexdigest()
    with service(tmp_path, clock) as state:
        issuer = CapabilityIssuer(state, token_factory=lambda: raw_token)
        issuer.mint(grant())
        with pytest.raises(ProjectionAuthorityError):
            issuer.mint(grant())
        assert len(state.query_events("run-1")) == 1
        assert state.revoke_capability(token_hash)

    with service(tmp_path, clock) as restarted:
        record = restarted.capability_record(token_hash)
        assert record is not None
        assert record.revoked_at == NOW


@pytest.mark.asyncio
async def test_discovery_does_not_replace_call_time_authorization(tmp_path: Path) -> None:
    clock = MutableClock()
    calls: list[AuthorizedCall] = []

    async def target(call: AuthorizedCall):
        calls.append(call)
        return {"worker_id": call.worker_id}

    with service(tmp_path, clock) as state:
        raw_token = CapabilityIssuer(state).mint(grant())
        gate = gateway(state, clock, target)
        assert gate.discover(session(raw_token)) == (
            GatewayTool.STATE_QUERY,
            GatewayTool.SCHEDULE_PROPOSE,
        )
        assert gate.discover(replace(session(raw_token), role="unknown")) == ()
        result = await gate.call(session(raw_token), GatewayTool.STATE_QUERY, {"query": "runs"})
        assert isinstance(result, GatewaySuccess)
        assert result.exit_code == 0
        assert result.result == {"worker_id": "worker-1"}

        assert state.revoke_capability(sha256(raw_token.encode()).hexdigest())
        denied = await gate.call(session(raw_token), GatewayTool.STATE_QUERY, {})
        expiring_token = CapabilityIssuer(state).mint(grant())
        assert gate.discover(session(expiring_token))
        clock.wall = NOW + timedelta(hours=1)
        expired = await gate.call(session(expiring_token), GatewayTool.STATE_QUERY, {})
        denial_events = [
            record.envelope.event_type for record in state.query_events("run-1")
        ]

    assert isinstance(denied, GatewayFailure)
    assert isinstance(expired, GatewayFailure)
    assert denied.exit_code == 4
    assert len(calls) == 1
    assert "CapabilityDenied" in denial_events


@pytest.mark.asyncio
async def test_limits_allow_exact_configured_count_before_dispatch_denial(tmp_path: Path) -> None:
    clock = MutableClock()
    calls = 0

    def target(call: AuthorizedCall):
        nonlocal calls
        calls += 1
        return {"request_id": call.request_id}

    with service(tmp_path, clock) as state:
        raw_token = CapabilityIssuer(state).mint(grant())
        rate_gate = gateway(state, clock, target, request_limit=2)
        assert isinstance(
            await rate_gate.call(session(raw_token), GatewayTool.STATE_QUERY, {}), GatewaySuccess
        )
        assert isinstance(
            await rate_gate.call(session(raw_token), GatewayTool.STATE_QUERY, {}), GatewaySuccess
        )
        rate_denied = await rate_gate.call(session(raw_token), GatewayTool.STATE_QUERY, {})

        budget_gate = gateway(state, clock, target, run_budget=1)
        assert isinstance(
            await budget_gate.call(session(raw_token), GatewayTool.STATE_QUERY, {}), GatewaySuccess
        )
        budget_denied = await budget_gate.call(session(raw_token), GatewayTool.STATE_QUERY, {})

    assert isinstance(rate_denied, GatewayFailure)
    assert isinstance(budget_denied, GatewayFailure)
    assert calls == 3


def test_capability_row_rolls_back_when_minted_event_projection_fails(tmp_path: Path) -> None:
    clock = MutableClock()

    def failing_reducer(
        snapshots: tuple[ProjectionRecord, ...], event: EventEnvelope
    ) -> tuple[ProjectionRecord, ...]:
        if event.event_type == "CapabilityMinted":
            raise InjectedMutationFailure
        return apply_event(snapshots, event)

    raw_token = "a" * 43
    with service(tmp_path, clock, reducer=failing_reducer) as state:
        issuer = CapabilityIssuer(state, token_factory=lambda: raw_token)
        with pytest.raises(InjectedMutationFailure):
            issuer.mint(grant())
        assert state.capability_record(sha256(raw_token.encode()).hexdigest()) is None
        assert state.query_events("run-1") == ()


@pytest.mark.parametrize("event_type", ["LeaseReleased", "LeaseRecovered"])
def test_lease_terminal_event_revokes_bound_tokens_in_same_transaction(
    tmp_path: Path, event_type: str
) -> None:
    clock = MutableClock()
    with service(tmp_path, clock) as state:
        raw_token = CapabilityIssuer(state).mint(grant(lease_id="lease-1"))
        token_hash = sha256(raw_token.encode()).hexdigest()
        state.append_event(
            AppendEventCommand(event_type, "broker", "run-1", None, {"lease_id": "lease-1"})
        )
        record = state.capability_record(token_hash)
        assert record is not None
        assert record.revoked_at == NOW


def test_failed_lease_terminal_event_does_not_revoke_token(tmp_path: Path) -> None:
    clock = MutableClock()

    def failing_reducer(
        snapshots: tuple[ProjectionRecord, ...], event: EventEnvelope
    ) -> tuple[ProjectionRecord, ...]:
        if event.event_type == "LeaseReleased":
            raise InjectedMutationFailure
        return apply_event(snapshots, event)

    with service(tmp_path, clock, reducer=failing_reducer) as state:
        raw_token = CapabilityIssuer(state).mint(grant(lease_id="lease-1"))
        token_hash = sha256(raw_token.encode()).hexdigest()
        with pytest.raises(InjectedMutationFailure):
            state.append_event(
                AppendEventCommand(
                    "LeaseReleased", "broker", "run-1", None, {"lease_id": "lease-1"}
                )
            )
        record = state.capability_record(token_hash)
        assert record is not None
        assert record.revoked_at is None
