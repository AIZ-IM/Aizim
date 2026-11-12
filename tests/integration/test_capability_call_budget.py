from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from integration.gateway_support import MutableClock, gateway, grant, service, session

from aizim.domain.serialization import JsonValue
from aizim.gateway import CapabilityIssuer, GatewayFailure, GatewaySuccess, GatewayTool
from aizim.orchestration.worker_authority import WorkerDirective
from aizim.state import StateService, StateServiceConfig
from aizim.state.rpc import RpcFailure, RpcRequest, RpcSuccess, rpc_call


@pytest.fixture
def rpc_project() -> Iterator[Path]:
    with TemporaryDirectory(prefix="aizim-budget-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.mark.parametrize("budget", [1, 2, 401])
async def test_minted_budget_limits_calls_across_tokens_for_one_worker(
    tmp_path: Path, budget: int,
) -> None:
    clock = MutableClock()
    dispatched = []

    def target(call):
        dispatched.append(call.worker_id)
        return {"worker_id": call.worker_id}

    with service(tmp_path, clock) as state:
        issuer = CapabilityIssuer(state)
        first = issuer.mint(replace(grant(), call_budget=budget))
        gate = gateway(state, clock, target, request_limit=10000,
                       run_budget=10000, worker_budget=10000)
        for _ in range(budget):
            assert isinstance(await gate.call(session(first), GatewayTool.STATE_QUERY, {}),
                              GatewaySuccess)
        # A new token for the same worker must not reset its consumed budget.
        refreshed = issuer.mint(replace(grant(), call_budget=budget))
        exhausted = await gate.call(session(refreshed), GatewayTool.STATE_QUERY, {})
        assert isinstance(exhausted, GatewayFailure)
        assert exhausted.error.code == "CAPABILITY_DENIED"
        assert state.query_events("run-1")[-1].envelope.payload["reason_code"] == (
            "WORKER_BUDGET_EXHAUSTED"
        )
        other = issuer.mint(replace(grant(), worker_id="worker-2", call_budget=budget))
        result = await gate.call(replace(session(other), worker_id="worker-2"),
                                 GatewayTool.STATE_QUERY, {})
        assert isinstance(result, GatewaySuccess)
        assert dispatched == ["worker-1"] * budget + ["worker-2"]


def test_minted_call_budget_survives_restart_and_replay(tmp_path: Path) -> None:
    config = StateServiceConfig(tmp_path, "budget-state")
    with StateService(config) as state:
        token = CapabilityIssuer(state).mint(replace(grant(), call_budget=750))
        token_hash = sha256(token.encode()).hexdigest()
        record = state.capability_record(token_hash)
        assert record is not None and record.call_budget == 750
        assert state.query_events("run-1")[-1].envelope.payload["call_budget"] == 750
        digest = state.logical_digest()

    with StateService(config) as restarted:
        assert restarted.capability_record(token_hash) == record
        assert restarted.logical_digest() == digest
        assert restarted.replay_verify().matched


def test_historical_capability_table_migrates_without_changing_history(tmp_path: Path) -> None:
    config = StateServiceConfig(tmp_path, "budget-migration")
    with StateService(config) as state:
        token = CapabilityIssuer(state).mint(grant())
        token_hash = sha256(token.encode()).hexdigest()
        original = state.capability_record(token_hash)
        original_events = state.query_events("run-1")
        digest = state.logical_digest()
    database = tmp_path / ".aizim" / "state.sqlite3"
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sqlite3, sys; "
            "connection = sqlite3.connect(sys.argv[1]); "
            "connection.execute('ALTER TABLE capability_tokens DROP COLUMN call_budget'); "
            "connection.close()",
            str(database),
        ],
        check=True,
    )

    with StateService(config) as migrated:
        assert migrated.capability_record(token_hash) == original
        assert original is not None and original.call_budget is None
        assert migrated.query_events("run-1") == original_events
        assert migrated.logical_digest() == digest
        assert migrated.replay_verify().matched
        new_token = CapabilityIssuer(migrated).mint(replace(grant(), call_budget=10000))
        new_hash = sha256(new_token.encode()).hexdigest()
        new_record = migrated.capability_record(new_hash)
        assert new_record is not None and new_record.call_budget == 10000

    with StateService(config) as reopened:
        assert reopened.capability_record(token_hash) == original
        assert reopened.capability_record(new_hash) == new_record
        assert reopened.replay_verify().matched


@pytest.mark.parametrize("budget", [0, -1, True, 1.5, "400", 10001])
def test_capability_and_directive_reject_invalid_budgets(budget: Any) -> None:
    with pytest.raises(ValueError, match="call_budget"):
        replace(grant(), call_budget=budget)
    with pytest.raises(ValueError, match="INVALID_WORKER_DIRECTIVE"):
        WorkerDirective("directive-1", "worker-1", grant().role, b"source", budget, 1.0)


@pytest.mark.parametrize("budget", [None, 1, 400, 10000])
async def test_capability_budget_event_uses_existing_trusted_rpc_boundary(
    rpc_project: Path, budget: int | None,
) -> None:
    config = StateServiceConfig(rpc_project, "trusted-budget-session")
    async with StateService(config) as state:
        payload: dict[str, JsonValue] = {"worker_id": "worker-1", "role": grant().role.value}
        if budget is not None:
            payload["call_budget"] = budget
        request = RpcRequest("append_event", {
            "event_type": "CapabilityMinted", "actor": "capability_issuer", "run_id": "run-1",
            "causation_id": None, "payload": payload,
        }, None)
        denied = await rpc_call(state.socket_path, request)
        assert isinstance(denied, RpcFailure)
        assert state.query_events("run-1") == ()
        accepted = await rpc_call(
            state.socket_path, replace(request, session_id="trusted-budget-session")
        )
        assert isinstance(accepted, RpcSuccess)
        assert state.query_events("run-1")[-1].envelope.payload.get("call_budget") == budget

    with StateService(config) as restarted:
        assert restarted.replay_verify().matched
