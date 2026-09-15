from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from aizim.agents import AgentResult
from aizim.agents.sandbox import (
    ProbeAttempt,
    ProbeOperation,
    ProbeRequest,
    append_probe_event,
)
from aizim.domain.serialization import JsonValue
from aizim.orchestration.worker_lifecycle import record_agent_result
from aizim.state import EventValidationError, StateService, StateServiceConfig
from aizim.state.events import EventEnvelope

_HASH: Final = "a" * 64
_HASH_CASES: Final = (
    ("SandboxProbeDenied", "policy_hash", {"probe_id": "p", "reason_code": "denied"}),
    ("SandboxProbePassed", "policy_hash", {"probe_id": "p"}),
    (
        "PromotionVerificationRecorded",
        "source_scan_hash",
        {
            "axiom_verification_hash": _HASH,
            "axiom_verification_verdict": "pass",
            "build_hash": _HASH,
            "build_verdict": "pass",
            "contribution_id": "c",
            "diagnostics_hash": _HASH,
            "diagnostics_verdict": "pass",
        },
    ),
    (
        "AgentRunCompleted",
        "policy_hash",
        {
            "execution_id": "e",
            "exit_code": 0,
            "final_message_hash": _HASH,
            "status": "submitted",
            "transport_event_hash": _HASH,
            "worker_id": "w",
        },
    ),
)


def _event(event_type: str, payload: dict[str, JsonValue]) -> EventEnvelope:
    return EventEnvelope(
        event_id="01J00000000000000000000000",
        schema_version=1,
        event_type=event_type,
        occurred_at=datetime(2026, 7, 22, tzinfo=UTC),
        actor="supervisor",
        run_id="run-1",
        causation_id=None,
        payload=payload,
    )


@pytest.mark.parametrize(("event_type", "field", "payload"), _HASH_CASES)
def test_foundation_hash_bindings_reject_non_lowercase_sha256(
    event_type: str, field: str, payload: dict[str, JsonValue]
) -> None:
    with pytest.raises(EventValidationError):
        _event(event_type, {**payload, field: "A" * 64})


@pytest.mark.parametrize(("event_type", "field", "payload"), _HASH_CASES)
def test_foundation_hash_bindings_accept_lowercase_sha256(
    event_type: str, field: str, payload: dict[str, JsonValue]
) -> None:
    assert _event(event_type, {**payload, field: _HASH}).event_type == event_type


def test_agent_result_exposes_optional_policy_contract_hash() -> None:
    assert tuple(field.name for field in fields(AgentResult))[-2:] == ("policy_hash", "usage")


def test_agent_result_persists_optional_policy_contract_hash(tmp_path: Path) -> None:
    result = AgentResult("worker-1", "submitted", "done", _HASH, _HASH, 0, _HASH)

    with StateService(StateServiceConfig(tmp_path, "policy-evidence")) as state:
        record_agent_result(state, "run-1", "execution-1", result)
        payload = state.query_events("run-1")[-1].envelope.payload

    assert payload["policy_hash"] == _HASH


def test_sandbox_probe_event_persists_policy_contract_hash(tmp_path: Path) -> None:
    with StateService(StateServiceConfig(tmp_path, "probe-policy-evidence")) as state:
        request = ProbeRequest(
            "probe-1",
            "run-1",
            tmp_path,
            tmp_path / "view",
            tmp_path / "scratch",
            tmp_path / "state.sqlite3",
            tmp_path / "unleased.lean",
            tmp_path / "shared.txt",
            tmp_path / "gateway.sock",
            "127.0.0.1",
            1,
            tmp_path / "view" / "Allowed.lean",
            _HASH,
            "AIZIM_SECRET",
            {},
            state,
        )
        attempt = ProbeAttempt(ProbeOperation.READ_STATE_DATABASE, "denied", "SANDBOX_ENFORCED")
        append_probe_event(request, attempt, _HASH)
        payload = state.query_events("run-1")[-1].envelope.payload

    assert payload["policy_hash"] == _HASH
