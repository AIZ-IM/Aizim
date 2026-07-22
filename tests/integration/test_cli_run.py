from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aizim.modes.manifest import SMOKE_TEST_LIMITATION

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"


def _cli(
    *arguments: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "aizim", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
        env=environment,
    )


def _copy_smoke(tmp_path: Path) -> Path:
    return Path(
        shutil.copytree(
            SMOKE_ROOT, tmp_path / "smoke_lean", ignore=shutil.ignore_patterns(".aizim")
        )
    )


@pytest.mark.lean_integration
def test_fake_shared_run_prints_summary_and_persists_terminal_status(tmp_path: Path) -> None:
    project = _copy_smoke(tmp_path)

    result = _cli(
        "run",
        "--project",
        str(project),
        "--profile",
        "autonomous-shared",
        "--backend",
        "fake",
    )
    status = _cli("status", "--project", str(project), "--json")

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "AIZIM RUN PASS",
        "backend=fake",
        "participation=formal_unassisted",
        "runtime=shared",
        "proof_workers=2",
        "lsp_instances=1",
        "human_interventions=0",
        "start_knowledge_epoch=0",
        "end_knowledge_epoch=2",
        "verified_declarations=2",
        SMOKE_TEST_LIMITATION,
    ]
    assert status.returncode == 0
    document = json.loads(status.stdout)
    assert document["epochs"]["knowledge_epoch"] == 2
    assert len(document["verified_declarations"]) == 2
    assert {row["state"]["event_type"] for row in document["workers"]} == {"WorkerStopped"}
    assert {row["state"]["event_type"] for row in document["leases"]} == {"LeaseReleased"}
    assert any(row["state"]["event_type"] == "LeanRuntimeStopped" for row in document["resources"])


def test_run_rejects_the_isolated_profile_before_creating_state(tmp_path: Path) -> None:
    project = _copy_smoke(tmp_path)

    result = _cli("run", "--project", str(project), "--profile", "isolated", "--backend", "fake")

    assert result.returncode == 2
    assert not (project / ".aizim").exists()


def test_codex_run_requires_an_explicit_model_selection(tmp_path: Path) -> None:
    project = _copy_smoke(tmp_path)
    environment = dict(os.environ)
    environment.pop("AIZIM_MODEL", None)

    result = _cli(
        "run",
        "--project",
        str(project),
        "--profile",
        "autonomous-shared",
        "--backend",
        "codex",
        environment=environment,
    )

    assert result.returncode == 2
    assert result.stderr == "aizim run: autonomous-shared run failed\n"
