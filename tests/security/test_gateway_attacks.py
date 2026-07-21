from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

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
    GatewayTool,
)
from aizim.state import AppendEventCommand, StateDependencies, StateService, StateServiceConfig
from aizim.state.events import event_as_dict
from aizim.state.operations import RpcRequest, RpcSuccess
from aizim.state.rpc import rpc_call

NOW = datetime(2026, 7, 21, 10, tzinfo=UTC)


@pytest.fixture
def project_root() -> Iterator[Path]:
    with TemporaryDirectory(prefix="aizim-", dir="/tmp") as directory:
        yield Path(directory)


def projection_request(name: str, entity_id: str) -> RpcRequest:
    return RpcRequest(
        "query_projection", {"projection_name": name, "entity_id": entity_id}, None
    )


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def utc_now(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return 10.0


class Ids:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"{self._value:026d}"


def grant() -> CapabilityGrant:
    return CapabilityGrant(
        run_id="run-1",
        worker_id="worker-1",
        role=AgentRole.RESEARCH_CONDUCTOR,
        lease_id="lease-1",
        operations=(GatewayTool.STATE_QUERY,),
        expires_at=NOW + timedelta(minutes=30),
    )


def presented(raw_token: str | None) -> CapabilitySession:
    return CapabilitySession(
        token=raw_token,
        run_id="run-1",
        worker_id="worker-1",
        role=AgentRole.RESEARCH_CONDUCTOR.value,
        lease_id="lease-1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attack", "reason"),
    [
        ("missing", "MISSING_TOKEN"),
        ("random", "UNKNOWN_TOKEN"),
        ("changed", "UNKNOWN_TOKEN"),
        ("expired", "TOKEN_EXPIRED"),
        ("revoked", "TOKEN_REVOKED"),
        ("wrong_role", "ROLE_MISMATCH"),
        ("wrong_worker", "WORKER_MISMATCH"),
        ("wrong_run", "RUN_MISMATCH"),
        ("wrong_lease", "LEASE_MISMATCH"),
        ("token_role", "ROLE_MISMATCH"),
        ("token_worker", "WORKER_MISMATCH"),
        ("token_run", "RUN_MISMATCH"),
        ("token_operation", "UNADVERTISED_OPERATION"),
        ("unadvertised", "UNADVERTISED_OPERATION"),
        ("released_replay", "TOKEN_REVOKED"),
    ],
)
async def test_authorization_attacks_are_redacted_audited_and_side_effect_free(
    project_root: Path, attack: str, reason: str
) -> None:
    clock = Clock()
    calls: list[AuthorizedCall] = []

    def target(call: AuthorizedCall):
        calls.append(call)
        return {"unexpected": True}

    async with StateService(
        StateServiceConfig(project_root, "trusted"),
        StateDependencies(clock=clock.utc_now, event_ids=Ids("event")),
    ) as state:
        raw_token = CapabilityIssuer(state).mint(grant())
        token_hash = sha256(raw_token.encode()).hexdigest()
        request = presented(raw_token)
        operation: GatewayTool | str = GatewayTool.STATE_QUERY
        supplied_token = raw_token

        if attack == "missing":
            request = presented(None)
            supplied_token = ""
        elif attack == "random":
            supplied_token = "z" * 43
            request = presented(supplied_token)
        elif attack == "changed":
            supplied_token = raw_token[:-1] + ("A" if raw_token[-1] != "A" else "B")
            request = presented(supplied_token)
        elif attack == "expired":
            clock.now = NOW + timedelta(hours=1)
        elif attack == "revoked":
            assert state.revoke_capability(token_hash)
        elif attack == "wrong_role":
            request = replace(request, role=AgentRole.LITERATURE_SCOUT.value)
        elif attack == "wrong_worker":
            request = replace(request, worker_id="worker-2")
        elif attack == "wrong_run":
            request = replace(request, run_id="run-2")
        elif attack == "wrong_lease":
            request = replace(request, lease_id="lease-2")
        elif attack == "token_role":
            request = replace(request, role=raw_token)
        elif attack == "token_worker":
            request = replace(request, worker_id=raw_token)
        elif attack == "token_run":
            request = replace(request, run_id=raw_token)
        elif attack == "token_operation":
            operation = raw_token
        elif attack == "unadvertised":
            operation = GatewayTool.STATE_APPEND
        elif attack == "released_replay":
            state.append_event(
                AppendEventCommand(
                    "LeaseReleased", "broker", "run-1", None, {"lease_id": "lease-1"}
                )
            )

        gate = CapabilityGateway(
            state=state,
            targets={GatewayTool.STATE_QUERY: target},
            limits=GatewayLimits(100, 60.0, 100),
            dependencies=CapabilityDependencies(
                clock=clock.utc_now,
                monotonic=clock.monotonic,
                request_ids=Ids("request"),
            ),
        )
        health = await rpc_call(state.socket_path, RpcRequest("health", {}, None))
        protected_requests = (
            projection_request("resources", "run-1"),
            projection_request("leases", "lease-1"),
        )
        before_protected = tuple(
            [await rpc_call(state.socket_path, request) for request in protected_requests]
        )
        before_digest = state.logical_digest()
        before_events = len(state.query_events())
        result = await gate.call(request, operation, {"secret-shaped": raw_token})
        after_events = state.query_events()
        after_protected = tuple(
            [await rpc_call(state.socket_path, request) for request in protected_requests]
        )

        assert isinstance(result, GatewayFailure)
        assert isinstance(health, RpcSuccess)
        assert result.exit_code == 4
        assert result.error.code == "CAPABILITY_DENIED"
        assert result.error.message == "operation is not allowed for this worker"
        assert state.logical_digest() == before_digest
        assert after_protected == before_protected
        assert len(after_events) == before_events + 1
        denial = after_events[-1].envelope

    assert calls == []
    assert denial.event_type == "CapabilityDenied"
    assert denial.payload["reason_code"] == reason
    assert set(denial.payload) == {"reason_code", "role", "worker_id", "operation", "request_id"}
    serialized = repr(event_as_dict(denial)) + repr(result) + repr(request)
    assert raw_token not in serialized
    assert token_hash not in serialized
    if supplied_token:
        assert supplied_token not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "targets", "reason"),
    [
        (GatewayTool.SCHEDULE_PROPOSE, {GatewayTool.SCHEDULE_PROPOSE: lambda call: None},
         "OPERATION_NOT_MINTED"),
        (GatewayTool.STATE_QUERY, {}, "TARGET_UNREGISTERED"),
        ("invented.operation", {}, "UNADVERTISED_OPERATION"),
    ],
)
async def test_operation_and_target_failures_never_fall_through(
    project_root: Path, operation: GatewayTool | str, targets, reason: str
) -> None:
    clock = Clock()
    with StateService(
        StateServiceConfig(project_root, "trusted"),
        StateDependencies(clock=clock.utc_now, event_ids=Ids("event")),
    ) as state:
        raw_token = CapabilityIssuer(state).mint(grant())
        gate = CapabilityGateway(
            state,
            targets,
            GatewayLimits(10, 60.0, 10),
            CapabilityDependencies(clock.utc_now, clock.monotonic, Ids("request")),
        )
        result = await gate.call(presented(raw_token), operation, {})
        denial = state.query_events()[-1].envelope

    assert isinstance(result, GatewayFailure)
    assert denial.payload["reason_code"] == reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "unsafe"),
    [
        ("worker_id", "/Users/example/private/secret.txt"),
        ("role", "research_conductor\nsecret"),
        ("run_id", "../../private/state.sqlite3"),
        ("operation", "tinysecret"),
    ],
)
async def test_denial_audit_labels_never_persist_untrusted_free_form_values(
    project_root: Path, field: str, unsafe: str
) -> None:
    clock = Clock()
    with StateService(
        StateServiceConfig(project_root, "trusted"),
        StateDependencies(clock=clock.utc_now, event_ids=Ids("event")),
    ) as state:
        raw_token = CapabilityIssuer(state).mint(grant())
        request = presented(None if field == "run_id" else raw_token)
        operation: GatewayTool | str = GatewayTool.STATE_QUERY
        if field == "operation":
            operation = unsafe
        else:
            request = replace(request, **{field: unsafe})
        result = await CapabilityGateway(
            state,
            {GatewayTool.STATE_QUERY: lambda call: None},
            GatewayLimits(10, 60.0, 10),
            CapabilityDependencies(clock.utc_now, clock.monotonic, Ids("request")),
        ).call(request, operation, {})
        denial = state.query_events()[-1].envelope

    document = event_as_dict(denial)
    assert isinstance(result, GatewayFailure)
    assert document["run_id"] != unsafe
    assert unsafe not in document["payload"].values()
    assert unsafe not in repr(document) + repr(result)
