from __future__ import annotations

from pathlib import Path

from aizim.domain import AgentRole
from aizim.state import StateService, StateServiceConfig
from aizim.state.control_operations import assign_task, configure_controller, register_worker


def test_legacy_controller_roster_and_assignments_replay_deterministically(
    tmp_path: Path,
) -> None:
    # Given
    with StateService(StateServiceConfig(tmp_path, "legacy-control-replay")) as state:
        configure_controller(state, "codex", "legacy-fixture-model")
        register_worker(state, "legacy-worker-1", AgentRole.FORMALIZER)

        # When
        first_version = assign_task(state, "legacy-worker-1", "prove the legacy fixture")
        second_version = assign_task(state, "legacy-worker-1", "verify the legacy fixture")
        verification = state.replay_verify()
        controller = state.query_projection("controller", "primary")
        roster = state.query_projection("worker_roster", "legacy-worker-1")
        assignment = state.query_projection("worker_assignments", "legacy-worker-1")

    # Then
    assert first_version == 1
    assert second_version == 2
    assert verification.matched
    assert controller is not None and controller.version == 1
    assert roster is not None and roster.version == 1
    assert assignment is not None and assignment.version == 2


def test_legacy_well_formed_unregistered_provider_replays_deterministically(
    tmp_path: Path,
) -> None:
    with StateService(StateServiceConfig(tmp_path, "legacy-future-provider")) as state:
        configure_controller(state, "future_provider-1", None)

        controller = state.query_projection("controller", "primary")
        verification = state.replay_verify()

    assert controller is not None
    assert b'"provider":"future_provider-1"' in controller.state_json
    assert verification.matched
