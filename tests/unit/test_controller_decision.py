from __future__ import annotations

import json
import math

import pytest

from aizim.domain import AgentRole, canonical_json, sha256_bytes
from aizim.domain.serialization import JsonValue
from aizim.orchestration.controller_backend import (
    MAX_CONTROLLER_INSTRUCTION_BYTES,
    BlockedDecision,
    ControllerBackendError,
    ControllerContext,
    DispatchDecision,
    RejectDecision,
    controller_context_bytes,
    parse_controller_decision,
)
from aizim.orchestration.fake_controller_backend import FakeControllerBackend


def _context(
    allowed_operations: tuple[str, ...] = ("lean.goal", "document.apply"),
) -> ControllerContext:
    return ControllerContext(
        assignment_id="assignment-1",
        task_version=2,
        task="Prove the bounded fixture.",
        worker_id="formalizer-1",
        role=AgentRole.FORMALIZER,
        project_id="project-1",
        base_epoch="a" * 64,
        knowledge_epoch=3,
        allowed_operations=allowed_operations,
        max_budget=3,
        max_timeout_seconds=15.0,
        controller_version=4,
    )


def _raw(value: JsonValue) -> bytes:
    return json.dumps(value, allow_nan=True).encode()


def test_context_bytes_expose_only_safe_operation_labels() -> None:
    # Given
    context = _context()

    # When
    result = controller_context_bytes(context)

    # Then
    assert result == canonical_json(
        {
            "assignment_id": "assignment-1",
            "task_version": 2,
            "task": "Prove the bounded fixture.",
            "worker_id": "formalizer-1",
            "role": "formalizer",
            "project_id": "project-1",
            "base_epoch": "a" * 64,
            "knowledge_epoch": 3,
            "allowed_operations": ("lean.goal", "document.apply"),
            "max_budget": 3,
            "max_timeout_seconds": 15.0,
        }
    )
    assert b"controller_version" not in result
    assert b'"allowed_operations":["lean.goal","document.apply"]' in result
    assert b"socket" not in result
    assert b"session" not in result
    assert b"token" not in result
    assert b"credential" not in result


@pytest.mark.parametrize(
    ("allowed_operations", "encoded_operations"),
    (
        ((), b'"allowed_operations":[]'),
        (
            (
                "knowledge.read",
                "document.apply",
                "lean.diagnostics",
                "lean.multi_attempt",
                "lean.goal",
                "project.read",
                "contribution.submit",
            ),
            (
                b'"allowed_operations":["knowledge.read","document.apply",'
                b'"lean.diagnostics","lean.multi_attempt","lean.goal",'
                b'"project.read","contribution.submit"]'
            ),
        ),
    ),
)
def test_context_bytes_preserve_safe_operation_label_order(
    allowed_operations: tuple[str, ...], encoded_operations: bytes
) -> None:
    # Given
    context = _context(allowed_operations)

    # When
    result = controller_context_bytes(context)

    # Then
    assert encoded_operations in result


@pytest.mark.parametrize(
    "allowed_operations",
    (
        ("/tmp/aizim/run/state.sock",),
        ("service-session-token",),
        ("capability.mint",),
        ("lean.goal", "lean.goal"),
        (
            "project.read",
            "lean.goal",
            "lean.multi_attempt",
            "lean.diagnostics",
            "document.apply",
            "contribution.submit",
            "knowledge.read",
            "untrusted.operation",
        ),
    ),
)
def test_context_bytes_reject_unsafe_operation_labels(
    allowed_operations: tuple[str, ...],
) -> None:
    # Given
    context = _context(allowed_operations)

    # When
    with pytest.raises(ControllerBackendError) as error:
        controller_context_bytes(context)

    # Then
    assert str(error.value) == "CONTROLLER_DECISION_INVALID"


def test_context_bytes_reject_unhashable_operation_label() -> None:
    # Given
    context = _context()
    object.__setattr__(context, "allowed_operations", (["unhashable"],))

    # When
    with pytest.raises(ControllerBackendError) as error:
        controller_context_bytes(context)

    # Then
    assert str(error.value) == "CONTROLLER_DECISION_INVALID"


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        (
            {
                "action": "dispatch",
                "worker_id": "formalizer-1",
                "instruction": "Use the supplied document.",
                "budget": 3,
                "timeout_seconds": 15.0,
            },
            DispatchDecision("dispatch", "formalizer-1", "Use the supplied document.", 3, 15.0),
        ),
        (
            {"action": "blocked", "reason_code": "DEPENDENCY_UNAVAILABLE"},
            BlockedDecision("blocked", "DEPENDENCY_UNAVAILABLE"),
        ),
        (
            {"action": "reject", "reason_code": "TASK_UNSAFE"},
            RejectDecision("reject", "TASK_UNSAFE"),
        ),
    ),
)
def test_parser_returns_each_valid_decision(
    payload: JsonValue,
    expected: DispatchDecision | BlockedDecision | RejectDecision,
) -> None:
    assert parse_controller_decision(_raw(payload), _context()) == expected


@pytest.mark.parametrize(
    "payload",
    (
        {
            "action": "dispatch",
            "worker_id": "formalizer-1",
            "instruction": "Use the supplied document.",
            "budget": 1,
            "timeout_seconds": 1.0,
            "role": "research_conductor",
        },
        {"action": "blocked", "reason_code": "NO_SAFE_ACTION", "tool": "shell"},
        {"action": "reject", "reason_code": "TASK_UNSAFE", "extra": "injection"},
    ),
)
def test_parser_rejects_every_action_with_an_unknown_key(payload: JsonValue) -> None:
    with pytest.raises(ControllerBackendError):
        parse_controller_decision(_raw(payload), _context())


@pytest.mark.parametrize(
    "payload",
    (
        {
            "action": "dispatch",
            "worker_id": "other-worker",
            "instruction": "Use the supplied document.",
            "budget": 1,
            "timeout_seconds": 1.0,
        },
        {
            "action": "dispatch",
            "worker_id": "formalizer-1",
            "instruction": "Use the supplied document.",
            "budget": 1,
            "timeout_seconds": math.nan,
        },
        {
            "action": "dispatch",
            "worker_id": "formalizer-1",
            "instruction": "Use the supplied document.",
            "budget": 4,
            "timeout_seconds": 1.0,
        },
        {
            "action": "dispatch",
            "worker_id": "formalizer-1",
            "instruction": "Use the supplied document.",
            "budget": 1,
            "timeout_seconds": 16.0,
        },
        {
            "action": "dispatch",
            "worker_id": "formalizer-1",
            "instruction": "\U0001f642" * (MAX_CONTROLLER_INSTRUCTION_BYTES // 4 + 1),
            "budget": 1,
            "timeout_seconds": 1.0,
        },
        "free-form model response",
        {"action": "dispatch"},
    ),
)
def test_parser_fails_closed_for_unsafe_or_malformed_values(payload: JsonValue) -> None:
    with pytest.raises(ControllerBackendError):
        parse_controller_decision(_raw(payload), _context())


def test_parser_rejects_malformed_json() -> None:
    with pytest.raises(ControllerBackendError):
        parse_controller_decision(b'{"action":', _context())


@pytest.mark.parametrize(
    "raw",
    (
        b'{"action":"blocked","action":"reject","reason_code":"TASK_UNSAFE"}',
        b'{"action":"blocked","reason_code":"DEPENDENCY_UNAVAILABLE","reason_code":"NO_SAFE_ACTION"}',
        (
            b'{"action":"dispatch","worker_id":"other-worker","worker_id":"formalizer-1",'
            b'"instruction":"Use the supplied document.","budget":1,"timeout_seconds":1.0}'
        ),
    ),
)
def test_parser_rejects_duplicate_decision_keys(raw: bytes) -> None:
    # Given
    context = _context()

    # When
    with pytest.raises(ControllerBackendError) as error:
        parse_controller_decision(raw, context)

    # Then
    assert str(error.value) == "CONTROLLER_DECISION_INVALID"


async def test_fake_backend_records_exact_canonical_context_bytes() -> None:
    # Given
    context = _context()
    decision = BlockedDecision("blocked", "NO_SAFE_ACTION")
    backend = FakeControllerBackend(decision)

    # When
    result = await backend.plan(context)

    # Then
    assert result == decision
    assert backend.identity.name == "fake"
    assert backend.identity.version == "deterministic-controller-v1"
    assert backend.identity.executable_sha256 == sha256_bytes(b"deterministic-controller-v1")
    assert backend.received_context_bytes == [controller_context_bytes(context)]
