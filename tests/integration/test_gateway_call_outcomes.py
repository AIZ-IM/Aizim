from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from integration.gateway_support import MutableClock, gateway, grant, service, session

from aizim.domain import AgentRole
from aizim.gateway import (
    AuthorizedCall,
    CapabilityDependencies,
    CapabilityGateway,
    CapabilityIssuer,
    GatewayFailure,
    GatewayLimits,
    GatewaySuccess,
    GatewayTool,
)
from aizim.gateway.transport import (
    GatewayChannelClaims,
    GatewayChannelContext,
    GatewayTransport,
    serve_gateway_channel,
)
from aizim.omp.tools import GatewayHostTools
from aizim.state.events import MonotoneUlidFactory


def _socket_path(tmp_path: Path) -> Path:
    suffix = sha256(str(tmp_path).encode()).hexdigest()[:12]
    return Path("/tmp") / f"aizim-{os.getpid()}-{suffix}.sock"


async def test_target_errors_return_structured_failures_and_are_audited(tmp_path: Path) -> None:
    clock = MutableClock()
    failures = iter((RuntimeError("UNVERIFIED_DOCUMENT_APPLY"), KeyError("/private/path")))

    async def target(call: AuthorizedCall) -> dict[str, str]:
        error = next(failures, None)
        if error is not None:
            raise error
        return {"request_id": call.request_id}

    with service(tmp_path, clock) as state:
        raw_token = CapabilityIssuer(state).mint(grant())
        gate = gateway(state, clock, target)
        coded = await gate.call(session(raw_token), GatewayTool.STATE_QUERY, {})
        unexpected = await gate.call(session(raw_token), GatewayTool.STATE_QUERY, {})
        recovered = await gate.call(session(raw_token), GatewayTool.STATE_QUERY, {})
        failed = [
            record.envelope
            for record in state.query_events("run-1")
            if record.envelope.event_type == "GatewayCallFailed"
        ]
        assert state.replay_verify().matched

    assert isinstance(coded, GatewayFailure)
    assert coded.error.code == "UNVERIFIED_DOCUMENT_APPLY"
    assert isinstance(unexpected, GatewayFailure)
    assert unexpected.error.code == "TOOL_FAILED"
    assert "/private/path" not in unexpected.error.message
    assert isinstance(recovered, GatewaySuccess)
    assert [event.payload["reason_code"] for event in failed] == [
        "UNVERIFIED_DOCUMENT_APPLY",
        "TOOL_FAILED",
    ]
    assert [event.payload["error_type"] for event in failed] == ["RuntimeError", "KeyError"]
    assert [event.payload["worker_id"] for event in failed] == ["worker-1", "worker-1"]
    assert coded.error.event_id == failed[0].event_id


@pytest.mark.parametrize(
    "code", ["CAPABILITY_DENIED", "GATEWAY_INTERNAL", "GATEWAY_PROTOCOL", "GATEWAY_UNAVAILABLE",
             "GATEWAY_RATE_LIMITED"]
)
async def test_targets_cannot_impersonate_gateway_owned_codes(tmp_path: Path, code: str) -> None:
    clock = MutableClock()

    async def target(call: AuthorizedCall) -> dict[str, str]:
        raise RuntimeError(code)

    with service(tmp_path, clock) as state:
        raw_token = CapabilityIssuer(state).mint(grant())
        result = await gateway(state, clock, target).call(
            session(raw_token), GatewayTool.STATE_QUERY, {}
        )

    assert isinstance(result, GatewayFailure)
    assert result.error.code == "TOOL_FAILED"


async def test_channel_keeps_serving_after_a_gateway_exception(tmp_path: Path) -> None:
    class FailingOnceGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def call(self, session, operation, payload):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("UNEXPECTED_GATEWAY_STATE")
            return GatewaySuccess({"call": self.calls})

    gateway = FailingOnceGateway()
    secret = b"channel-secret-0123456789abcdef"
    path = _socket_path(tmp_path)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        context = GatewayChannelContext(
            gateway,
            GatewayChannelClaims("run-1", "worker-1", AgentRole.PROOF_EXPLORER, "lease-1"),
            bytearray(secret),
        )
        try:
            await serve_gateway_channel(reader, writer, context)
        finally:
            writer.close()

    server = await asyncio.start_unix_server(handle, str(path))
    try:
        reader, writer = await asyncio.open_unix_connection(str(path))
        transport = GatewayTransport(
            reader, writer, AgentRole.PROOF_EXPLORER, bytearray(secret), (GatewayTool.LEAN_GOAL,)
        )
        try:
            first = await transport.call(GatewayTool.LEAN_GOAL, {})
            second = await transport.call(GatewayTool.LEAN_GOAL, {})
        finally:
            await transport.aclose()
    finally:
        server.close()
        await server.wait_closed()
        with suppress(FileNotFoundError):
            path.unlink()

    assert isinstance(first, GatewayFailure)
    assert first.error.code == "GATEWAY_INTERNAL"
    assert first.error.event_id is None
    assert second == GatewaySuccess({"call": 2})
    assert gateway.calls == 2


async def test_worker_budget_limits_each_worker_independently(tmp_path: Path) -> None:
    clock = MutableClock()

    async def target(call: AuthorizedCall) -> dict[str, str]:
        return {"worker_id": call.worker_id}

    with service(tmp_path, clock) as state:
        issuer = CapabilityIssuer(state)
        first = issuer.mint(grant())
        second = issuer.mint(replace(grant(), worker_id="worker-2"))
        gate = gateway(state, clock, target, run_budget=10, worker_budget=2)
        spent = [await gate.call(session(first), GatewayTool.STATE_QUERY, {}) for _ in range(3)]
        other = await gate.call(
            replace(session(second), worker_id="worker-2"), GatewayTool.STATE_QUERY, {}
        )
        denials = [
            record.envelope.payload["reason_code"]
            for record in state.query_events("run-1")
            if record.envelope.event_type == "CapabilityDenied"
        ]

    assert [type(result) for result in spent] == [GatewaySuccess, GatewaySuccess, GatewayFailure]
    assert other == GatewaySuccess({"worker_id": "worker-2"})
    assert denials == ["WORKER_BUDGET_EXHAUSTED"]


async def test_audits_of_long_research_run_ids_stay_under_their_run(tmp_path: Path) -> None:
    # Research run ids are as long as tokens; the issuer persisted them, so they stay readable.
    run_id = "research-" + MonotoneUlidFactory()().lower()
    clock = MutableClock()

    async def target(call: AuthorizedCall) -> dict[str, str]:
        raise RuntimeError("UNVERIFIED_DOCUMENT_APPLY")

    with service(tmp_path, clock) as state:
        raw_token = CapabilityIssuer(state).mint(replace(grant(), run_id=run_id))
        presented = replace(session(raw_token), run_id=run_id)
        gate = gateway(state, clock, target, worker_budget=1)
        for _ in range(2):
            await gate.call(presented, GatewayTool.STATE_QUERY, {})
        audited = [
            (record.envelope.event_type, record.envelope.payload["reason_code"])
            for record in state.query_events(run_id)
            if record.envelope.event_type in {"GatewayCallFailed", "CapabilityDenied"}
        ]

    assert audited == [
        ("GatewayCallFailed", "UNVERIFIED_DOCUMENT_APPLY"),
        ("CapabilityDenied", "WORKER_BUDGET_EXHAUSTED"),
    ]


@pytest.mark.parametrize("worker_budget", [0, -1, True, 2.0])
def test_gateway_limits_reject_an_invalid_worker_budget(worker_budget: Any) -> None:
    with pytest.raises(ValueError, match="worker_budget"):
        GatewayLimits(10, 60.0, 10, worker_budget)


@pytest.mark.parametrize("revoked_during_backoff", [False, True])
async def test_worker_retries_rate_limit_over_live_channel_with_fresh_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revoked_during_backoff: bool,
) -> None:
    clock = MutableClock()
    calls = []
    delays = []
    path = _socket_path(tmp_path)

    def target(call: AuthorizedCall) -> dict[str, str]:
        calls.append(call.request_id)
        query = call.payload["query"]
        assert isinstance(query, str)
        return {"query": query}

    with service(tmp_path, clock) as state:
        issuer = CapabilityIssuer(state)
        raw_token = issuer.mint(grant())
        gate = gateway(state, clock, target, request_limit=1, run_budget=2, worker_budget=2)
        assert isinstance(await gate.call(
            session(raw_token), GatewayTool.STATE_QUERY, {"query": "first"}
        ), GatewaySuccess)
        clock.monotonic += 10.25

        async def backoff(delay: float) -> None:
            delays.append(delay)
            clock.monotonic += delay
            if revoked_during_backoff:
                state.revoke_capability(sha256(raw_token.encode()).hexdigest())

        monkeypatch.setattr("aizim.omp.tools.asyncio.sleep", backoff)

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            context = GatewayChannelContext(
                gate,
                GatewayChannelClaims("run-1", "worker-1", AgentRole.RESEARCH_CONDUCTOR, None),
                bytearray(raw_token.encode()),
            )
            try:
                await serve_gateway_channel(reader, writer, context)
            finally:
                writer.close()

        server = await asyncio.start_unix_server(handle, str(path))
        try:
            reader, writer = await asyncio.open_unix_connection(str(path))
            transport = GatewayTransport(
                reader, writer, AgentRole.RESEARCH_CONDUCTOR, bytearray(raw_token.encode()),
                (GatewayTool.STATE_QUERY,),
            )
            try:
                tools = GatewayHostTools(transport)
                text, failed = await tools("state_query", {"payload": {"query": "continued"}})
            finally:
                await transport.aclose()
        finally:
            server.close()
            await server.wait_closed()
            with suppress(FileNotFoundError):
                path.unlink()

        denials = [
            event.envelope.payload["reason_code"] for event in state.query_events("run-1")
            if event.envelope.event_type == "CapabilityDenied"
        ]
        assert delays == [49.75]
        assert denials == (["REQUEST_RATE_EXCEEDED", "TOKEN_REVOKED"]
                           if revoked_during_backoff else ["REQUEST_RATE_EXCEEDED"])
        assert failed is revoked_during_backoff
        assert tools.failed is revoked_during_backoff
        assert len(calls) == (1 if revoked_during_backoff else 2)
        if revoked_during_backoff:
            assert json.loads(text)["error"]["code"] == "CAPABILITY_DENIED"
        else:
            assert json.loads(text) == {"ok": True, "result": {"query": "continued"}}


async def test_rate_limit_retry_hint_is_capped_for_long_windows(tmp_path: Path) -> None:
    clock = MutableClock()
    with service(tmp_path, clock) as state:
        raw_token = CapabilityIssuer(state).mint(grant())
        gate = CapabilityGateway(
            state, {GatewayTool.STATE_QUERY: lambda call: {}}, GatewayLimits(1, 3600.0, 10),
            CapabilityDependencies(clock.utc_now, clock.monotonic_now, lambda: "request-1"),
        )
        await gate.call(session(raw_token), GatewayTool.STATE_QUERY, {})
        throttled = await gate.call(session(raw_token), GatewayTool.STATE_QUERY, {})
        assert isinstance(throttled, GatewayFailure)
        assert throttled.error.code == "GATEWAY_RATE_LIMITED"
        assert throttled.error.retry_after_seconds == 60.0
        assert throttled.error.event_id == state.query_events("run-1")[-1].envelope.event_id
