from __future__ import annotations

import asyncio  # noqa: ANYIO_OK -- drives the asyncio state RPC client
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from aizim.domain.serialization import JsonValue
from aizim.state.operations import RpcRequest, RpcSuccess
from aizim.state.rpc import rpc_call

FIXTURE = Path(__file__).parents[1] / "fixtures" / "minimal_lean"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "aizim", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def initialized_project(tmp_path: Path) -> Path:
    root = Path(shutil.copytree(FIXTURE, tmp_path / "lean-project"))
    assert run_cli("init", str(root)).returncode == 0
    return root


def wait_for(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists() and process.poll() is None


def rpc_projection(
    socket_path: Path, name: str, entity_id: str
) -> dict[str, JsonValue]:
    response = asyncio.run(
        rpc_call(
            socket_path,
            RpcRequest(
                operation="query_projection",
                params={"projection_name": name, "entity_id": entity_id},
                session_id=None,
            ),
        )
    )
    assert isinstance(response, RpcSuccess)
    assert type(response.result) is dict
    return response.result


@pytest.mark.parametrize(
    ("provider", "model"),
    (("codex", "gpt-5.6-sol"), ("claude", None)),
)
def test_controller_provider_is_configurable_and_persistent(
    tmp_path: Path, provider: str, model: str | None
) -> None:
    # Given
    root = initialized_project(tmp_path)
    command = [
        "controller",
        "configure",
        "--project",
        str(root),
        "--provider",
        provider,
    ]
    if model is not None:
        command.extend(("--model", model))

    # When
    configured = run_cli(*command)
    shown = run_cli("controller", "show", "--project", str(root), "--json")

    # Then
    assert configured.returncode == 0
    assert configured.stdout == f"Configured primary controller with {provider}\n"
    assert shown.returncode == 0
    assert json.loads(shown.stdout) == {
        "controller_id": "primary",
        "model": model,
        "provider": provider,
        "version": 1,
    }


def test_controller_assigns_versioned_tasks_to_persistent_workers(tmp_path: Path) -> None:
    # Given
    root = initialized_project(tmp_path)
    assert (
        run_cli(
            "controller",
            "configure",
            "--project",
            str(root),
            "--provider",
            "codex",
            "--model",
            "gpt-5.6-sol",
        ).returncode
        == 0
    )
    registered = run_cli(
        "worker",
        "register",
        "--project",
        str(root),
        "--worker-id",
        "counterexample-a",
        "--role",
        "counterexample_agent",
    )

    # When
    first = run_cli(
        "worker",
        "assign",
        "--project",
        str(root),
        "--worker-id",
        "counterexample-a",
        "--task",
        "search quartic families",
    )
    second = run_cli(
        "worker",
        "assign",
        "--project",
        str(root),
        "--worker-id",
        "counterexample-a",
        "--task",
        "verify the surviving family",
    )
    listed = run_cli("worker", "list", "--project", str(root), "--json")

    # Then
    assert registered.returncode == 0
    assert registered.stdout == "Registered worker counterexample-a\n"
    assert first.returncode == 0
    assert first.stdout == "Assigned task version 1 to counterexample-a\n"
    assert second.returncode == 0
    assert second.stdout == "Assigned task version 2 to counterexample-a\n"
    assert listed.returncode == 0
    document = json.loads(listed.stdout)
    assert document["controller"] == {
        "controller_id": "primary",
        "model": "gpt-5.6-sol",
        "provider": "codex",
        "version": 1,
    }
    assert document["workers"] == [
        {
            "assignment": {
                "assignment_id": document["workers"][0]["assignment"]["assignment_id"],
                "controller_id": "primary",
                "task": "verify the surviving family",
                "task_hash": hashlib.sha256(
                    b"verify the surviving family"
                ).hexdigest(),
                "task_version": 2,
            },
            "role": "counterexample_agent",
            "status": "idle",
            "version": 1,
            "worker_id": "counterexample-a",
        }
    ]
    assert len(document["workers"][0]["assignment"]["assignment_id"]) == 64


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


def test_worker_control_rejects_duplicate_and_unregistered_assignment(
    tmp_path: Path,
) -> None:
    # Given
    root = initialized_project(tmp_path)
    assert (
        run_cli(
            "controller",
            "configure",
            "--project",
            str(root),
            "--provider",
            "claude",
        ).returncode
        == 0
    )
    registration = (
        "worker",
        "register",
        "--project",
        str(root),
        "--worker-id",
        "proof-a",
        "--role",
        "formalizer",
    )
    assert run_cli(*registration).returncode == 0

    # When
    duplicate = run_cli(*registration)
    unknown = run_cli(
        "worker",
        "assign",
        "--project",
        str(root),
        "--worker-id",
        "missing",
        "--task",
        "prove the target",
    )
    listed = run_cli("worker", "list", "--project", str(root), "--json")

    # Then
    assert duplicate.returncode == 4
    assert duplicate.stderr == "aizim worker: worker is already registered\n"
    assert unknown.returncode == 4
    assert unknown.stderr == "aizim worker: worker is not registered\n"
    assert len(json.loads(listed.stdout)["workers"]) == 1
