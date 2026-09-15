from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from aizim.agents import FakeAgentBackend
from aizim.cli.init_command import run_init
from aizim.config.model import ResourcePolicy
from aizim.domain.serialization import JsonValue
from aizim.gateway import connect_gateway
from aizim.orchestration.resources import ResourceGovernor
from aizim.orchestration.worker_host import WorkerExecutionHost
from aizim.research.engine import LeanExecutor, ResearchEngine
from aizim.research.records import define_task, require_task
from aizim.state import StateService, StateServiceConfig

_ROOT = Path(__file__).parents[2]


@pytest.mark.lean_integration
@pytest.mark.parametrize("lake_style", ("toml", "lean"))
async def test_real_lean_proves_a_non_smoke_research_target(
    tmp_path: Path, lake_style: str
) -> None:
    project = Path(shutil.copytree(_ROOT / "tests/fixtures/research_lean", tmp_path / "research"))
    if lake_style == "lean":
        (project / "lakefile.toml").unlink()
        (project / "lakefile.lean").write_text(
            "import Lake\nopen Lake DSL\npackage research_lab\n"
            "@[default_target]\nlean_lib ResearchLab\n"
        )
    imports: list[JsonValue] = ["ResearchLab"] if lake_style == "lean" else ["Std"]
    assert run_init(project) == 0
    with StateService(StateServiceConfig(project, "research-lean-test")) as state:
        define_task(
            state,
            {
                "task_id": "addition",
                "title": "Addition identity",
                "statement": "(n : Nat) : n + 0 = n",
                "author": "Researcher",
                "imports": imports,
                "depends_on": [],
                "source_refs": [],
                "max_rounds": 2,
                "max_failures": 2,
                "timeout_seconds": 120,
            },
        )
        governor = ResourceGovernor(
            ResourcePolicy(max_proof_workers=1, scratch_slots=1), disk_free=lambda _: 3_000_000_000
        )
        host = await WorkerExecutionHost.open(
            state, project, project, governor, "research-lean-test", continue_on_failure=True
        )
        try:
            await host.prewarm()
            fixture = json.loads(
                (_ROOT / "src/aizim/fixtures/fake_workers/prover_a.json").read_text()
            )
            fixture["rounds"][0][3]["payload"]["imports"] = imports
            fixture_path = tmp_path / "worker.json"
            fixture_path.write_text(json.dumps(fixture))
            backend = FakeAgentBackend.from_fixture(
                fixture_path,
                connect=connect_gateway,
                cursor={},
            )
            executor = LeanExecutor(state, project, host, backend, "deterministic-tool-actions")
            await ResearchEngine(
                state, executor, "research-lean-test", "deterministic-tool-actions", workers=1
            ).run()
            task = require_task(state, "addition")
            assert task["status"] == "verified", task["feedback"]
            assert task["rounds"] == 1
            assert task["verified_declarations"]
            assert state.replay_verify().matched
            assert "AizimSmoke" not in str(task)
        finally:
            await host.aclose()
