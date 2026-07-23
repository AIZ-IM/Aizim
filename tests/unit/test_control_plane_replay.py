from __future__ import annotations

from pathlib import Path

from aizim.domain import AgentRole
from aizim.orchestration.control_plane import (
    ControllerProvider,
    assign_task,
    configure_controller,
    register_worker,
)
from aizim.state import StateService, StateServiceConfig


def test_controller_roster_and_assignments_replay_deterministically(
    tmp_path: Path,
) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, "control-replay")) as state:
        configure_controller(state, ControllerProvider.CODEX, "fixture-model")
        register_worker(state, "worker-1", AgentRole.FORMALIZER)

        # When
        first_version = assign_task(state, "worker-1", "prove the fixture")
        second_version = assign_task(state, "worker-1", "verify the fixture")
        verification = state.replay_verify()
        controller = state.query_projection("controller", "primary")
        roster = state.query_projection("worker_roster", "worker-1")
        assignment = state.query_projection("worker_assignments", "worker-1")

    # Then
    assert first_version == 1
    assert second_version == 2
    assert verification.matched
    assert controller is not None and controller.version == 1
    assert roster is not None and roster.version == 1
    assert assignment is not None and assignment.version == 2
