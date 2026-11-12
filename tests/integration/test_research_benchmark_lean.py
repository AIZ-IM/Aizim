from __future__ import annotations

import json
from pathlib import Path

import pytest

from aizim.research.benchmark import BenchmarkOptions, run_benchmark
from aizim.research.benchmark_ladder import load_ladder
from aizim.research.records import object_value, records, text, texts
from aizim.state import StateService, StateServiceConfig
from aizim.state.event_payload import thaw_payload


@pytest.mark.lean_integration
async def test_scripted_ladder_uses_real_lean_and_published_dependencies(tmp_path: Path) -> None:
    destination, metrics = await run_benchmark(tmp_path, BenchmarkOptions())
    assert metrics["reason"] == "", metrics
    assert metrics["verified_targets"] == metrics["targets"] == 7, metrics
    assert metrics["verify_rate"] == 1.0
    assert metrics["charged_rounds"] == 7
    assert metrics["charged_calls"] == metrics["gateway_requests"] == 35
    assert metrics["infrastructure_failure_rate"] == 0.0
    assert object_value(metrics["usage"])["estimated_cost_usd"] is None
    assert json.loads(destination.read_text()) == metrics
    project = destination.parent / "lean-project"
    with StateService(StateServiceConfig(project, "inspect-benchmark")) as state:
        attempts = records(state, "attempt")
        tasks = records(state, "task")
        assert len(attempts) == len(tasks) == 7
        assert state.replay_verify().matched
        published = {
            text(thaw_payload(record.envelope.payload)["declaration_id"]): record.envelope
            for record in state.query_events(text(metrics["run_id"]))
            if record.envelope.event_type == "DeclarationPublished"
        }
        assert len(published) == 7
        verified: dict[str, list[str]] = {
            text(task["task_id"]): texts(task["verified_declarations"]) for task in tasks
        }
        for target in load_ladder().targets:
            assert len(verified[target.task_id]) == 1
            declaration = published[verified[target.task_id][0]]
            assert declaration.actor == "promotion_service"
            dependencies = texts(thaw_payload(declaration.payload)["dependencies"])
            for dependency in target.depends_on:
                parent = published[verified[dependency][0]]
                assert thaw_payload(parent.payload)["module"] in dependencies
        runs = records(state, "run")
        assert len(runs) == 1
        assert runs[0]["status"] == "finished"
        assert runs[0]["finished_at"]
