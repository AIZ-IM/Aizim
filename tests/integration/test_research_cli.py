from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from aizim.research.report import snapshot

FIXTURE = Path(__file__).parents[1] / "fixtures/research_lean"


def cli(project: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "aizim", "research", *arguments, "--project", str(project)],
        capture_output=True,
        text=True,
    )


def test_public_cli_records_human_roles_and_exports_an_explorable_record(tmp_path: Path) -> None:
    project = Path(shutil.copytree(FIXTURE, tmp_path / "project"))
    initialized = subprocess.run(
        [sys.executable, "-m", "aizim", "init", str(project)], capture_output=True
    )
    assert initialized.returncode == 0
    result = cli(
        project,
        "task",
        "add",
        "--task-id",
        "addition",
        "--title",
        "Identity",
        "--statement",
        "(n : Nat) : n + 0 = n",
        "--author",
        "Ada",
        "--source-ref",
        "local:note",
    )
    assert result.returncode == 0, result.stderr
    for arguments in (
        (
            "inbox",
            "add",
            "--task-id",
            "addition",
            "--author",
            "Ada",
            "--body",
            "Keep the Nat formulation.",
        ),
        (
            "memory",
            "add",
            "--task-id",
            "addition",
            "--kind",
            "dead_end",
            "--body",
            "Induction is unnecessary.",
        ),
        (
            "contribution",
            "add",
            "--task-id",
            "addition",
            "--author",
            "Ada",
            "--role",
            "problem_formulation",
            "--body",
            "I selected the natural-number statement.",
        ),
        (
            "contribution",
            "add",
            "--task-id",
            "addition",
            "--author",
            "Ada",
            "--role",
            "exposition",
            "--body",
            "</script><script>alert('x')</script>",
        ),
        (
            "review",
            "add",
            "--task-id",
            "addition",
            "--author",
            "Ada",
            "--kind",
            "semantic_alignment",
            "--verdict",
            "accept",
            "--body",
            "This matches the intended question.",
        ),
    ):
        response = cli(project, *arguments)
        assert response.returncode == 0, response.stderr
    found = cli(project, "memory", "search", "induction", "--task-id", "addition")
    assert found.returncode == 0 and "unnecessary" in found.stdout
    found = cli(project, "search", "addition_identity", "--mode", "name", "--project-only")
    assert found.returncode == 0 and "ResearchLab" in found.stdout
    listed = cli(project, "task", "list")
    assert json.loads(listed.stdout)[0]["status"] == "pending"
    exported = tmp_path / "research.html"
    response = cli(project, "export", "--output", str(exported))
    assert response.returncode == 0, response.stderr
    assert stat.S_IMODE(exported.stat().st_mode) == 0o600
    contents = exported.read_text()
    assert "problem_formulation" in contents and "operator_declared" in contents
    assert "</script><script>alert" not in contents
    assert "\\u003c/script\\u003e" in contents
    assert cli(project, "export", "--output", str(exported)).returncode != 0
    data = snapshot(project)
    assert data["publication"] == "local_private_record"
    assert data["formal_evidence"] == []


def test_pause_is_durable_and_does_not_rewrite_the_target(tmp_path: Path) -> None:
    project = Path(shutil.copytree(FIXTURE, tmp_path / "project"))
    assert (
        subprocess.run(
            [sys.executable, "-m", "aizim", "init", str(project)], capture_output=True
        ).returncode
        == 0
    )
    assert (
        cli(
            project,
            "task",
            "add",
            "--task-id",
            "test",
            "--title",
            "Question",
            "--statement",
            "True",
        ).returncode
        == 0
    )
    before = json.loads(cli(project, "task", "list").stdout)[0]
    assert cli(project, "task", "pause", "--task-id", "test", "--author", "Ada").returncode == 0
    paused = json.loads(cli(project, "task", "list").stdout)[0]
    assert paused["paused"] and paused["target_hash"] == before["target_hash"]
    assert cli(project, "task", "resume", "--task-id", "test").returncode == 0
    assert not json.loads(cli(project, "task", "list").stdout)[0]["paused"]


def test_public_cli_rejects_a_proof_in_place_of_the_target(tmp_path: Path) -> None:
    project = Path(shutil.copytree(FIXTURE, tmp_path / "project"))
    assert (
        subprocess.run(
            [sys.executable, "-m", "aizim", "init", str(project)], capture_output=True
        ).returncode
        == 0
    )
    result = cli(
        project,
        "task",
        "add",
        "--task-id",
        "wrong",
        "--title",
        "Question",
        "--statement",
        "True := by trivial",
    )
    assert result.returncode != 0
    assert json.loads(cli(project, "task", "list").stdout) == []


def test_research_mutations_use_the_live_state_service() -> None:
    with TemporaryDirectory(prefix="aizim-research-rpc-", dir="/tmp") as directory:
        project = Path(shutil.copytree(FIXTURE, Path(directory) / "project"))
        assert (
            subprocess.run(
                [sys.executable, "-m", "aizim", "init", str(project)], capture_output=True
            ).returncode
            == 0
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "aizim", "state", "serve", "--project", str(project)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 10
            while not (project / ".aizim/run/state.sock").exists():
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.02)
            result = cli(
                project,
                "task",
                "add",
                "--task-id",
                "live",
                "--title",
                "Live target",
                "--statement",
                "True",
            )
            assert result.returncode == 0, result.stderr
            result = cli(
                project,
                "inbox",
                "add",
                "--task-id",
                "live",
                "--body",
                "Check the intended statement.",
            )
            assert result.returncode == 0, result.stderr
            assert (
                json.loads(cli(project, "inbox", "list").stdout)[0]["body"]
                == "Check the intended statement."
            )
        finally:
            process.terminate()
            process.wait(timeout=10)
        assert json.loads(cli(project, "task", "list").stdout)[0]["task_id"] == "live"
