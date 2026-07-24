from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from integration.cli_control_support import (
    PublicControlCommand,
    ReplacementTarget,
    StaticOwnershipState,
    initialized_project,
    replacing_rpc_server,
    rpc_projection,
    run_cli,
    run_public_control,
    static_ownership_records,
    wait_for,
)

from aizim.state import StateService, StateServiceConfig


def test_live_owner_accepts_public_control_commands_and_remains_owner() -> None:
    # Given
    with TemporaryDirectory(prefix="aizim-control-", dir="/tmp") as directory:
        root = initialized_project(Path(directory))
        command = [
            sys.executable,
            "-m",
            "aizim",
            "state",
            "serve",
            "--project",
            str(root),
        ]
        owner = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        socket_path = root / ".aizim" / "run" / "state.sock"
        pid_path = root / ".aizim" / "run" / "state.pid"
        try:
            wait_for(socket_path, owner)
            owner_pid = pid_path.read_text().strip()

            # When
            configured = run_cli(
                "controller",
                "configure",
                "--project",
                str(root),
                "--provider",
                "codex",
                "--model",
                "gpt-5.6-sol",
            )
            registered = run_cli(
                "worker",
                "register",
                "--project",
                str(root),
                "--worker-id",
                "worker-1",
                "--role",
                "formalizer",
            )
            assigned = run_cli(
                "worker",
                "assign",
                "--project",
                str(root),
                "--worker-id",
                "worker-1",
                "--task",
                "prove the fixture",
            )

            # Then
            assert configured.returncode == registered.returncode == assigned.returncode == 0
            assert owner.poll() is None
            assert pid_path.read_text().strip() == owner_pid
            controller = rpc_projection(socket_path, "controller", "primary")
            roster = rpc_projection(socket_path, "worker_roster", "worker-1")
            assignment = rpc_projection(socket_path, "worker_assignments", "worker-1")
            assert controller["version"] == 1
            assert roster["version"] == 1
            assert assignment["version"] == 1
        finally:
            owner.terminate()
            owner.wait(timeout=5)


@pytest.mark.parametrize("command", ("configure", "register", "assign"))
@pytest.mark.parametrize(
    "ownership_state",
    ("partial_pid", "partial_socket", "stale", "unsafe_pid", "unsafe_socket"),
)
def test_public_control_commands_fail_closed_for_invalid_ownership_records(
    command: PublicControlCommand,
    ownership_state: StaticOwnershipState,
) -> None:
    # Given
    with TemporaryDirectory(prefix="aizim-ownership-", dir="/tmp") as directory:
        root = initialized_project(Path(directory))
        run_root = root / ".aizim" / "run"
        pid_path = run_root / "state.pid"
        socket_path = run_root / "state.sock"
        with StateService(StateServiceConfig(root, "direct-owner")) as owner:
            before = owner.logical_digest()
            listeners = static_ownership_records(run_root, ownership_state)
            try:
                # When
                result = run_public_control(root, command)
            finally:
                for listener in listeners:
                    listener.close()
                pid_path.unlink(missing_ok=True)
                socket_path.unlink(missing_ok=True)

            # Then
            surface = "controller" if command == "configure" else "worker"
            assert result.returncode == 6
            assert result.stderr == f"aizim {surface}: state is unavailable\n"
            assert owner.logical_digest() == before


@pytest.mark.parametrize("command", ("configure", "register", "assign"))
@pytest.mark.parametrize("replacement_target", ("pid", "socket"))
def test_public_control_commands_fail_closed_when_live_identity_is_replaced(
    command: PublicControlCommand,
    replacement_target: ReplacementTarget,
) -> None:
    # Given
    with TemporaryDirectory(prefix="aizim-replaced-", dir="/tmp") as directory:
        root = initialized_project(Path(directory))
        run_root = root / ".aizim" / "run"
        pid_path = run_root / "state.pid"
        socket_path = run_root / "state.sock"
        with StateService(StateServiceConfig(root, "direct-owner")) as owner:
            before = owner.logical_digest()
            listener, thread, replacements = replacing_rpc_server(
                pid_path, socket_path, replacement_target
            )
            try:
                # When
                result = run_public_control(root, command)
                thread.join(timeout=2)

                # Then
                surface = "controller" if command == "configure" else "worker"
                assert not thread.is_alive()
                assert result.returncode == 6
                assert result.stderr == f"aizim {surface}: state is unavailable\n"
                assert owner.logical_digest() == before
            finally:
                listener.close()
                for replacement in replacements:
                    replacement.close()
                pid_path.unlink(missing_ok=True)
                socket_path.unlink(missing_ok=True)
