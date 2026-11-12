from __future__ import annotations

import argparse
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from aizim.cli import research_command
from aizim.cli.init_command import run_init
from aizim.domain.serialization import JsonValue
from aizim.research.records import get, operator_change, require_task
from aizim.runtime.layout import ProjectLayout
from aizim.state import StateService, StateServiceConfig


@pytest.fixture
def research_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, StateService]]:
    fixture = Path(__file__).parents[1] / "fixtures/research_lean"
    project = Path(shutil.copytree(fixture, tmp_path / "project"))
    assert run_init(project) == 0
    with StateService(StateServiceConfig(project, "cli-execution-limits")) as state:
        def control(layout: ProjectLayout, operation: str, params: dict[str, JsonValue]) -> int:
            assert layout.root == project
            assert operation == "control.research"
            return operator_change(state, params)

        monkeypatch.setattr(research_command, "call_control_operation", control)
        yield project, state


def cli(project: Path, *arguments: str) -> int:
    parser = argparse.ArgumentParser()
    research_command.add_research_parser(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(["research", *arguments, "--project", str(project)])
    return research_command.run_research_command(args)


def add(project: Path, *arguments: str) -> int:
    return cli(
        project, "task", "add", "--task-id", "addition", "--title", "Addition identity",
        "--statement", "(n : Nat) : n + 0 = n", *arguments,
    )


def test_research_cli_defaults_to_400_calls_and_900_seconds(
    research_state: tuple[Path, StateService],
) -> None:
    project, state = research_state
    assert add(project) == 0
    task = require_task(state, "addition")
    assert task["call_budget"] == 400
    assert task["timeout_seconds"] == 900


def test_research_cli_adds_and_raises_target_call_budget(
    research_state: tuple[Path, StateService], capsys: pytest.CaptureFixture[str]
) -> None:
    project, state = research_state
    assert add(project, "--call-budget", "17", "--timeout-seconds", "1200") == 0
    original = require_task(state, "addition")
    assert original["call_budget"] == 17
    assert original["timeout_seconds"] == 1200
    assert cli(project, "task", "pause", "--task-id", "addition") == 0
    assert cli(project, "task", "resume", "--task-id", "addition", "--call-budget", "10000") == 0
    resumed = require_task(state, "addition")
    assert resumed["call_budget"] == 10000
    assert resumed["paused"] is False
    assert resumed["target_hash"] == original["target_hash"]
    assert cli(project, "task", "resume", "--task-id", "addition", "--call-budget", "17") == 4
    assert "TASK_CALL_BUDGET_CANNOT_DECREASE" in capsys.readouterr().err
    assert require_task(state, "addition")["call_budget"] == 10000


@pytest.mark.parametrize("budget", ["0", "10001"])
def test_research_cli_rejects_out_of_range_call_budgets(
    research_state: tuple[Path, StateService], budget: str
) -> None:
    project, state = research_state
    assert add(project, "--call-budget", budget) == 4
    assert get(state, "task", "addition") is None


@pytest.mark.parametrize(("timeout", "expected"), [("0", 4), ("1", 0), ("14400", 0), ("14401", 4)])
def test_research_cli_preserves_timeout_bounds(
    research_state: tuple[Path, StateService], timeout: str, expected: int
) -> None:
    project, state = research_state
    assert add(project, "--timeout-seconds", timeout) == expected
    task = get(state, "task", "addition")
    if expected == 0:
        assert task is not None and task["timeout_seconds"] == int(timeout)
    else:
        assert task is None
