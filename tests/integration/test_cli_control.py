from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from integration.cli_control_support import initialized_project, run_cli


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
        "runtime": None,
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
        "runtime": None,
        "version": 1,
    }
    assert document["workers"] == [
        {
            "assignment": {
                "assignment_id": document["workers"][0]["assignment"]["assignment_id"],
                "controller_id": "primary",
                "execution": None,
                "task": "verify the surviving family",
                "task_hash": hashlib.sha256(b"verify the surviving family").hexdigest(),
                "task_version": 2,
            },
            "role": "counterexample_agent",
            "status": "idle",
            "version": 1,
            "worker_id": "counterexample-a",
        }
    ]
    assert len(document["workers"][0]["assignment"]["assignment_id"]) == 64


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


def test_human_control_output_includes_compact_lifecycle_state(tmp_path: Path) -> None:
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
    assert (
        run_cli(
            "worker",
            "register",
            "--project",
            str(root),
            "--worker-id",
            "proof-a",
            "--role",
            "formalizer",
        ).returncode
        == 0
    )
    assert (
        run_cli(
            "worker",
            "assign",
            "--project",
            str(root),
            "--worker-id",
            "proof-a",
            "--task",
            "prove the target",
        ).returncode
        == 0
    )

    shown = run_cli("controller", "show", "--project", str(root))
    listed = run_cli("worker", "list", "--project", str(root))

    assert shown.stdout == "primary provider=codex model=gpt-5.6-sol runtime=inactive\n"
    assert listed.stdout == (
        "controller codex runtime=inactive\n"
        "proof-a role=formalizer status=idle task_version=1 execution=pending\n"
    )
