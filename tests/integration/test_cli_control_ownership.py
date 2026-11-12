from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from integration.cli_control_support import (
    ReplacementTarget,
    StaticOwnershipState,
    add_research_task,
    initialized_project,
    replacing_rpc_server,
    run_cli,
    static_ownership_records,
    wait_for,
)

from aizim.state import StateService, StateServiceConfig


def test_live_owner_accepts_research_mutations_and_status_reads() -> None:
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
            added = add_research_task(root)
            status = run_cli("status", "--project", str(root), "--json")
            listed = run_cli("research", "task", "list", "--project", str(root))

            # Then
            assert added.returncode == status.returncode == listed.returncode == 0
            assert owner.poll() is None
            assert pid_path.read_text().strip() == owner_pid
            assert json.loads(added.stdout) == {"id": "ownership-task", "version": 1}
            assert json.loads(listed.stdout)[0]["task_id"] == "ownership-task"
        finally:
            owner.terminate()
            owner.wait(timeout=5)


@pytest.mark.parametrize(
    "ownership_state",
    ("partial_pid", "partial_socket", "stale", "unsafe_pid", "unsafe_socket"),
)
def test_research_mutations_fail_closed_for_invalid_ownership_records(
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
                result = add_research_task(root)
            finally:
                for listener in listeners:
                    listener.close()
                pid_path.unlink(missing_ok=True)
                socket_path.unlink(missing_ok=True)

            # Then
            reason = (
                "live state ownership record is incomplete"
                if ownership_state in {"partial_pid", "partial_socket"}
                else "live state service is unavailable"
            )
            assert result.returncode == 4
            assert result.stderr == f"aizim research: {reason}\n"
            assert owner.logical_digest() == before


@pytest.mark.parametrize("replacement_target", ("pid", "socket"))
def test_research_mutations_fail_closed_when_live_identity_is_replaced(
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
                result = add_research_task(root)
                thread.join(timeout=2)

                # Then
                assert not thread.is_alive()
                assert result.returncode == 4
                assert result.stderr == (
                    "aizim research: live state ownership changed during control request\n"
                )
                assert owner.logical_digest() == before
            finally:
                listener.close()
                for replacement in replacements:
                    replacement.close()
                pid_path.unlink(missing_ok=True)
                socket_path.unlink(missing_ok=True)
