from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, assert_never

import pytest

type Provider = Literal["codex", "claude"]


def _selected(provider: Provider) -> bool:
    selected = os.environ.get("AIZIM_REAL_CONTROLLER_PROVIDER")
    return selected in {provider, "all"}


def _auth_available(provider: Provider) -> bool:
    match provider:
        case "codex":
            return any(os.environ.get(name) for name in ("OPENAI_API_KEY", "CODEX_HOME"))
        case "claude":
            return any(
                os.environ.get(name) for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
            )
        case unreachable:
            assert_never(unreachable)


def _inputs(provider: Provider) -> tuple[Path, str, str]:
    project = os.environ.get("AIZIM_REAL_CONTROLLER_PROJECT")
    controller_model = os.environ.get("AIZIM_REAL_CONTROLLER_MODEL")
    worker_model = os.environ.get("AIZIM_REAL_WORKER_MODEL")
    if (
        not _selected(provider)
        or not _auth_available(provider)
        or project is None
        or controller_model is None
        or worker_model is None
    ):
        pytest.skip("AUTH_UNAVAILABLE")
    source = Path(project).expanduser()
    if not source.is_dir():
        pytest.skip("AUTH_UNAVAILABLE")
    return source, controller_model, worker_model


def _cli(
    environment: dict[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "aizim", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
        env=environment,
    )


def _require_cli(
    environment: dict[str, str],
    *arguments: str,
) -> None:
    assert _cli(environment, *arguments).returncode == 0


def _terminal(environment: dict[str, str], root: Path) -> bool:
    result = _cli(environment, "worker", "list", "--project", str(root), "--json")
    if result.returncode != 0:
        return False
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    if type(value) is not dict:
        return False
    workers = value.get("workers")
    if type(workers) is not list or len(workers) != 1 or type(workers[0]) is not dict:
        return False
    assignment = workers[0].get("assignment")
    if type(assignment) is not dict:
        return False
    execution = assignment.get("execution")
    return type(execution) is dict and execution.get("status") in {
        "completed",
        "failed",
        "interrupted",
    }


def _wait_for_terminal(
    environment: dict[str, str],
    root: Path,
    process: subprocess.Popen[str],
) -> None:
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        if _terminal(environment, root):
            return
        assert process.poll() is None
        time.sleep(1)
    pytest.fail("real controller did not reach a terminal assignment")


@pytest.mark.manual_real_controller
@pytest.mark.parametrize("provider", ("codex", "claude"))
def test_real_controller_reaches_one_terminal_assignment(provider: Provider) -> None:
    source, controller_model, worker_model = _inputs(provider)
    environment = dict(os.environ)
    environment["AIZIM_MODEL"] = worker_model
    with TemporaryDirectory(prefix="aizim-real-controller-", dir="/tmp") as directory:
        root = Path(
            shutil.copytree(
                source,
                Path(directory) / "lean-project",
                ignore=shutil.ignore_patterns(".aizim", ".lake"),
            )
        )
        _require_cli(environment, "init", str(root))
        _require_cli(
            environment,
            "controller",
            "configure",
            "--project",
            str(root),
            "--provider",
            provider,
            "--model",
            controller_model,
        )
        _require_cli(
            environment,
            "worker",
            "register",
            "--project",
            str(root),
            "--worker-id",
            "proof-a",
            "--role",
            "formalizer",
        )
        _require_cli(
            environment,
            "worker",
            "assign",
            "--project",
            str(root),
            "--worker-id",
            "proof-a",
            "--task",
            "Dispatch proof-a to prove a deterministic True theorem.",
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "aizim",
                "controller",
                "start",
                "--project",
                str(root),
                "--foreground",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            _wait_for_terminal(environment, root, process)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=30)
        listed = _cli(environment, "worker", "list", "--project", str(root), "--json")
        assert listed.returncode == 0
        document = json.loads(listed.stdout)
        assert len(document["workers"]) == 1
        assert document["workers"][0]["assignment"]["execution"]["status"] in {
            "completed",
            "failed",
            "interrupted",
        }
