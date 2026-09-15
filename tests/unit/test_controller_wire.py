from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from aizim.domain import AgentRole
from aizim.orchestration.controller_backend import (
    BlockedDecision,
    ControllerBackendError,
    ControllerContext,
    DispatchDecision,
    parse_controller_wire_decision,
)
from aizim.orchestration.controller_process import private_workspace
from aizim.orchestration.controller_transport import prepare_auth


def context() -> ControllerContext:
    return ControllerContext(
        "assignment",
        1,
        "prove True",
        "proof-a",
        AgentRole.PROOF_EXPLORER,
        "project",
        "a" * 64,
        0,
        ("lean.goal",),
        12,
        30.0,
        1,
    )


def test_wire_nulls_normalize_into_the_strict_local_decision() -> None:
    blocked = {
        "action": "blocked",
        "worker_id": None,
        "instruction": None,
        "budget": None,
        "timeout_seconds": None,
        "reason_code": "NO_SAFE_ACTION",
    }
    assert parse_controller_wire_decision(
        json.dumps(blocked).encode(), context()
    ) == BlockedDecision("blocked", "NO_SAFE_ACTION")
    dispatch = {
        "action": "dispatch",
        "worker_id": "proof-a",
        "instruction": "Try rfl.",
        "budget": 4,
        "timeout_seconds": 20,
        "reason_code": None,
    }
    assert parse_controller_wire_decision(
        json.dumps(dispatch).encode(), context()
    ) == DispatchDecision("dispatch", "proof-a", "Try rfl.", 4, 20.0)


@pytest.mark.parametrize("extra", [{"unknown": None}, {"worker_id": "wrong-worker"}])
def test_wire_normalization_never_discards_unknown_or_inappropriate_fields(extra) -> None:
    value = {"action": "blocked", "reason_code": "NO_SAFE_ACTION", **extra}
    with pytest.raises(ControllerBackendError):
        parse_controller_wire_decision(json.dumps(value).encode(), context())


def test_wire_budget_and_duplicate_fields_remain_rejected() -> None:
    raw = b'{"action":"blocked","action":"reject","reason_code":"NO_SAFE_ACTION"}'
    with pytest.raises(ControllerBackendError):
        parse_controller_wire_decision(raw, context())
    value = {
        "action": "dispatch",
        "worker_id": "proof-a",
        "instruction": "Try rfl.",
        "budget": 13,
        "timeout_seconds": 20,
        "reason_code": None,
    }
    with pytest.raises(ControllerBackendError):
        parse_controller_wire_decision(json.dumps(value).encode(), context())


def test_research_timeout_uses_the_trusted_task_ceiling() -> None:
    value = {
        "action": "dispatch",
        "worker_id": "proof-a",
        "instruction": "Try rfl.",
        "budget": 4,
        "timeout_seconds": 120,
        "reason_code": None,
    }
    raw = json.dumps(value).encode()
    decision = parse_controller_wire_decision(raw, replace(context(), max_timeout_seconds=120))
    assert isinstance(decision, DispatchDecision) and decision.timeout_seconds == 120
    with pytest.raises(ControllerBackendError):
        parse_controller_wire_decision(raw, context())


def test_backend_failure_survives_context_manager_cleanup() -> None:
    with (
        pytest.raises(ControllerBackendError, match="CONTROLLER_DECISION_INVALID"),
        private_workspace("aizim-error-test-"),
    ):
        raise ControllerBackendError("CONTROLLER_DECISION_INVALID")


def test_auth_copy_is_private_and_excludes_settings_and_history(tmp_path: Path) -> None:
    home = tmp_path / "home"
    original = home / ".codex"
    original.mkdir(parents=True)
    (original / "auth.json").write_text('{"fixture":"authentication"}')
    (original / "config.toml").write_text('model = "must-not-copy"')
    (original / "history.jsonl").write_text("must-not-copy")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    key, target = prepare_auth("codex", {"HOME": str(home)}, scratch)
    assert key == "CODEX_HOME"
    assert (target / "auth.json").read_bytes() == (original / "auth.json").read_bytes()
    assert stat.S_IMODE((target / "auth.json").stat().st_mode) == 0o600
    assert not (target / "config.toml").exists()
    assert not (target / "history.jsonl").exists()
    assert not target.is_relative_to(scratch / "tmp")


def test_auth_copy_rejects_symlinked_credentials(tmp_path: Path) -> None:
    auth = tmp_path / "auth"
    auth.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("private")
    (auth / "auth.json").symlink_to(outside)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(OSError):
        prepare_auth("codex", {"CODEX_HOME": str(auth)}, scratch)
