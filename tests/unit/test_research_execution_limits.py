from __future__ import annotations

from pathlib import Path

import pytest

from aizim.domain.serialization import JsonValue
from aizim.research.records import (
    ResearchError,
    define_task,
    get,
    operator_change,
    require_task,
    save,
    task_hash,
    valid_record,
)
from aizim.state import StateService, StateServiceConfig


def task_params() -> dict[str, JsonValue]:
    return {
        "task_id": "addition",
        "title": "Addition identity",
        "statement": "(n : Nat) : n + 0 = n",
        "author": "Ada",
        "imports": ["Std"],
        "depends_on": [],
        "source_refs": [],
        "max_rounds": 6,
        "max_failures": 3,
        "timeout_seconds": 900,
    }


def historical_task() -> dict[str, JsonValue]:
    params = task_params()
    return {
        **params,
        "status": "pending",
        "rounds": 0,
        "failures": 0,
        "target_hash": task_hash(params),
        "feedback": "",
        "verified_declarations": [],
        "paused": False,
    }


def historical_attempt() -> dict[str, JsonValue]:
    return {
        "id": "attempt-one",
        "task_id": "addition",
        "worker_id": "worker-one",
        "run_id": "run-one",
        "target_hash": task_hash(task_params()),
        "round": 1,
        "status": "failed",
        "feedback": "No proof found.",
        "verified_declarations": [],
        "usage": None,
        "model": "test-model",
        "inbox_ids": [],
    }


def resume(state: StateService, budget: JsonValue) -> int:
    return operator_change(state, {
        "action": "task.resume",
        "body": {"task_id": "addition", "author": "Ada", "call_budget": budget},
    })


@pytest.mark.parametrize("budget", [1, 400, 10000])
def test_call_budget_is_bounded_and_outside_the_immutable_target_hash(
    tmp_path: Path, budget: int
) -> None:
    with StateService(StateServiceConfig(tmp_path, "budget-test")) as state:
        define_task(state, {**task_params(), "call_budget": budget})
        task = require_task(state, "addition")
        assert task["call_budget"] == budget
        assert task["attempt_sequence"] == 0
        assert task["target_hash"] == task_hash(task_params())


@pytest.mark.parametrize("budget", [0, 10001, True, 1.5, "400", None])
def test_task_creation_rejects_invalid_call_budgets(tmp_path: Path, budget: JsonValue) -> None:
    with StateService(StateServiceConfig(tmp_path, "invalid-budget")) as state:
        with pytest.raises(ResearchError, match="INVALID_RESEARCH_RECORD"):
            define_task(state, {**task_params(), "call_budget": budget})
        assert get(state, "task", "addition") is None


def test_resume_raises_call_budget_without_rewriting_the_target(tmp_path: Path) -> None:
    with StateService(StateServiceConfig(tmp_path, "resume-budget")) as state:
        define_task(state, task_params())
        original = require_task(state, "addition")
        assert original["call_budget"] == 400
        resume(state, 10000)
        resume(state, 10000)
        with pytest.raises(ResearchError, match="TASK_CALL_BUDGET_CANNOT_DECREASE"):
            resume(state, 9999)
        updated = require_task(state, "addition")
        assert updated["call_budget"] == 10000
        assert updated["target_hash"] == original["target_hash"]
        assert updated["statement"] == original["statement"]


@pytest.mark.parametrize("budget", [0, 10001, True])
def test_resume_rejects_invalid_call_budgets(tmp_path: Path, budget: JsonValue) -> None:
    with StateService(StateServiceConfig(tmp_path, "resume-invalid-budget")) as state:
        define_task(state, task_params())
        with pytest.raises(ResearchError, match="INVALID_INTEGER"):
            resume(state, budget)
        assert require_task(state, "addition")["call_budget"] == 400


def test_historical_records_replay_and_resume_uses_the_historical_default(tmp_path: Path) -> None:
    with StateService(StateServiceConfig(tmp_path, "historical-budget")) as state:
        save(state, "task", "addition", historical_task(), actor="fixture")
        save(state, "attempt", "attempt-one", historical_attempt(), actor="fixture")
        assert state.replay_verify().matched
    with StateService(StateServiceConfig(tmp_path, "historical-restart")) as state:
        historical = require_task(state, "addition")
        assert "call_budget" not in historical
        assert "attempt_sequence" not in historical
        attempt = get(state, "attempt", "attempt-one")
        assert attempt is not None and "charged" not in attempt
        with pytest.raises(ResearchError, match="TASK_CALL_BUDGET_CANNOT_DECREASE"):
            resume(state, 399)
        resume(state, 401)
        assert require_task(state, "addition")["call_budget"] == 401
        assert state.replay_verify().matched


@pytest.mark.parametrize("sequence", [0, 1, 10000])
def test_task_attempt_sequence_is_nonnegative(sequence: int) -> None:
    task = {**historical_task(), "attempt_sequence": sequence}
    assert valid_record({"record_id": "task:addition", "kind": "task", "data": task})


@pytest.mark.parametrize("sequence", [-1, True, 1.5, "1"])
def test_task_rejects_invalid_attempt_sequences(sequence: JsonValue) -> None:
    task = {**historical_task(), "attempt_sequence": sequence}
    assert not valid_record({"record_id": "task:addition", "kind": "task", "data": task})


def test_uncharged_infrastructure_attempt_and_sequence_replay(tmp_path: Path) -> None:
    task = {**historical_task(), "attempt_sequence": 7, "rounds": 2}
    attempt = {
        **historical_attempt(),
        "round": 7,
        "infrastructure_failure": True,
        "charged": False,
        "finished_at": "2026-11-11T12:00:00+00:00",
    }
    with StateService(StateServiceConfig(tmp_path, "uncharged-attempt")) as state:
        save(state, "task", "addition", task, actor="fixture")
        save(state, "attempt", "attempt-one", attempt, actor="fixture")
        assert require_task(state, "addition")["rounds"] == 2
        stored = get(state, "attempt", "attempt-one")
        assert stored is not None and stored["round"] == 7 and stored["charged"] is False
        assert state.replay_verify().matched


@pytest.mark.parametrize(("field", "value"), [
    ("infrastructure_failure", 1),
    ("infrastructure_failure", "true"),
    ("charged", 0),
    ("charged", None),
    ("finished_at", "2026-11-11T12:00:00"),
    ("finished_at", "2026-11-11T12:00:00+01:00"),
    ("finished_at", "invalid"),
    ("finished_at", ""),
    ("call_budget", 0),
    ("call_budget", 10001),
    ("call_budget", True),
    ("call_budget", "400"),
])
def test_attempt_rejects_invalid_optional_execution_fields(field: str, value: JsonValue) -> None:
    attempt = {**historical_attempt(), field: value}
    assert not valid_record({
        "record_id": "attempt:attempt-one", "kind": "attempt", "data": attempt,
    })


@pytest.mark.parametrize("finished_at", ["2026-11-11T12:00:00Z", "2026-11-11T12:00:00+00:00"])
def test_attempt_accepts_utc_completion_times(finished_at: str) -> None:
    attempt = {
        **historical_attempt(),
        "infrastructure_failure": False,
        "charged": True,
        "finished_at": finished_at,
    }
    assert valid_record({"record_id": "attempt:attempt-one", "kind": "attempt", "data": attempt})


@pytest.mark.parametrize("budget", [1, 400, 10000])
def test_attempt_accepts_its_own_bounded_budget_snapshot(budget: int) -> None:
    attempt = {**historical_attempt(), "call_budget": budget}
    assert valid_record({"record_id": "attempt:attempt-one", "kind": "attempt", "data": attempt})
