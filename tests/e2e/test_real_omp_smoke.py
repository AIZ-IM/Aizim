from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aizim.knowledge.deltas import research_name
from aizim.orchestration.run_identity import candidate_name
from aizim.state import StateService, StateServiceConfig

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "research_lean"
_TASK_ID = "real-omp-true"


def _cli(project: Path, transcript: list[str], *arguments: str) -> str:
    command = [sys.executable, "-m", "aizim", *arguments]
    try:
        result = subprocess.run(
            command,
            cwd=project,
            check=False,
            capture_output=True,
            text=True,
            timeout=900,
            env=dict(os.environ),
        )
    except subprocess.TimeoutExpired as error:
        transcript.append(
            f"{command!r}\ntimed out\nstdout:\n{error.stdout!r}\nstderr:\n{error.stderr!r}"
        )
        pytest.fail("\n\n".join(transcript), pytrace=False)
    transcript.append(
        f"{command!r}\nexit: {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert result.returncode == 0, "\n\n".join(transcript)
    return result.stdout


@pytest.mark.manual_real_omp
@pytest.mark.lean_integration
@pytest.mark.skipif(
    os.environ.get("AIZIM_REAL_OMP") != "1",
    reason="Set AIZIM_REAL_OMP=1 to use installed omp, magpie, and real Lean services",
)
def test_real_omp_research_publishes_the_target_and_does_not_dispatch_it_twice(
    tmp_path: Path,
) -> None:
    project = Path(
        shutil.copytree(
            _FIXTURE,
            tmp_path / "research",
            ignore=shutil.ignore_patterns(".aizim", ".lake"),
        )
    )
    transcript: list[str] = []
    _cli(project, transcript, "init", str(project))
    _cli(
        project,
        transcript,
        "research", "task", "add",
        "--project", str(project),
        "--task-id", _TASK_ID,
        "--title", "Prove True through the Lean gateway",
        "--statement", "True",
        "--max-rounds", "1",
        "--max-failures", "1",
        "--timeout-seconds", "180",
    )
    _cli(
        project, transcript, "research", "run", "--project", str(project),
        "--max-seconds", "600",
    )
    listed = _cli(project, transcript, "research", "task", "list", "--project", str(project))
    status = _cli(project, transcript, "status", "--project", str(project), "--json")
    exported = tmp_path / "research.json"
    _cli(
        project, transcript, "research", "export", "--project", str(project),
        "--format", "json", "--output", str(exported),
    )

    try:
        tasks = json.loads(listed)
        document = json.loads(status)
        report = json.loads(exported.read_text())
        assert len(tasks) == 1
        task = tasks[0]
        assert task["task_id"] == _TASK_ID
        assert task["statement"] == "True"
        assert task["status"] == "verified", task
        assert task["rounds"] == 1
        assert report["records"]["task"] == tasks
        attempts = report["records"]["attempt"]
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt["task_id"] == _TASK_ID
        assert attempt["status"] == "verified", attempt
        assert attempt["target_hash"] == task["target_hash"]
        assert attempt["round"] == 1
        run_id, worker_id = attempt["run_id"], attempt["worker_id"]
        assert report["records"]["run"][0]["id"] == run_id
        assert report["records"]["run"][0]["status"] == "finished"

        with StateService(StateServiceConfig(project, "real-omp-read")) as state:
            events = state.query_events()
            replay = state.replay_verify()
            assert replay.matched, replay
        run_events = [record.envelope for record in events if record.envelope.run_id == run_id]
        submitted = [event for event in run_events if event.event_type == "ContributionSubmitted"]
        assert len(submitted) == 1
        contribution = submitted[0].payload
        assert contribution["worker_id"] == worker_id
        submitted_candidate = contribution["candidate_name"]
        payload_hash = contribution["payload_hash"]
        assert isinstance(submitted_candidate, str)
        assert isinstance(payload_hash, str)
        assert submitted_candidate == candidate_name(run_id, worker_id)
        assert contribution["complete_type"] == "True"
        published = [event for event in run_events if event.event_type == "DeclarationPublished"]
        assert len(published) == 1
        declaration = published[0].payload
        assert declaration["contribution_id"] == contribution["contribution_id"]
        assert declaration["type"] == "True"
        assert declaration["name"] == research_name(
            submitted_candidate, payload_hash, "AizimResearch"
        )
        declaration_ids = [declaration["declaration_id"]]
        assert task["verified_declarations"] == declaration_ids
        assert attempt["verified_declarations"] == declaration_ids
        assert [row["entity_id"] for row in document["verified_declarations"]] == declaration_ids
        assert [
            row["state"] for row in document["verified_declarations"]
        ] == report["formal_evidence"]
        assert report["formal_evidence"][0]["payload"]["declaration_id"] == declaration_ids[0]
        assert report["formal_evidence"][0]["payload"]["name"] == declaration["name"]
        assert report["formal_evidence"][0]["payload"]["type"] == "True"
        verifications = [
            event.payload for event in run_events
            if event.event_type == "PromotionVerificationRecorded"
        ]
        assert len(verifications) == 1
        verification = verifications[0]
        assert verification["contribution_id"] == contribution["contribution_id"]
        for stage in ("diagnostics", "build", "source_scan", "axiom_verification"):
            assert verification[f"{stage}_verdict"] == "pass", verification
            digest = verification[f"{stage}_hash"]
            assert isinstance(digest, str) and len(digest) == 64
        assert verification["source_scan_hash"] == verification["axiom_verification_hash"]
        started = [
            event.payload["worker_id"] for event in run_events
            if event.event_type == "WorkerStarted"
        ]
        stopped = [
            event.payload["worker_id"] for event in run_events
            if event.event_type == "WorkerStopped"
        ]
        assert started == stopped == [worker_id]
        granted: list[str] = []
        released: list[str] = []
        for event in run_events:
            if event.event_type in {"LeaseGranted", "LeaseReleased"}:
                lease_id = event.payload["lease_id"]
                assert isinstance(lease_id, str)
                if event.event_type == "LeaseGranted":
                    granted.append(lease_id)
                else:
                    released.append(lease_id)
        assert granted and sorted(granted) == sorted(released)
        assert {row["state"]["event_type"] for row in document["leases"]} == {"LeaseReleased"}
        assert {row["state"]["event_type"] for row in document["workers"]} == {"WorkerStopped"}
        assert not any(
            event.event_type in {"WorkerCrashed", "WorkerTimedOut", "PromotionFailed"}
            for event in run_events
        )
    except Exception as error:
        pytest.fail(f"{type(error).__name__}: {error}\n\n" + "\n\n".join(transcript), pytrace=True)

    _cli(
        project, transcript, "research", "run", "--project", str(project),
        "--max-seconds", "600",
    )
    listed_again = _cli(project, transcript, "research", "task", "list", "--project", str(project))
    status_again = _cli(project, transcript, "status", "--project", str(project), "--json")
    exported_again = tmp_path / "research-again.json"
    _cli(
        project, transcript, "research", "export", "--project", str(project),
        "--format", "json", "--output", str(exported_again),
    )
    try:
        after = json.loads(exported_again.read_text())
        assert json.loads(listed_again) == tasks
        assert after["records"]["attempt"] == attempts
        assert after["formal_evidence"] == report["formal_evidence"]
        assert len(after["records"]["run"]) == 2
        second_runs = [run for run in after["records"]["run"] if run["id"] != run_id]
        assert len(second_runs) == 1 and second_runs[0]["status"] == "finished"
        second_run_id = second_runs[0]["id"]
        with StateService(StateServiceConfig(project, "real-omp-read-again")) as state:
            after_events = state.query_events()
            replay = state.replay_verify()
            assert replay.matched, replay
        dispatch_types = {"WorkerStarted", "ContributionSubmitted", "DeclarationPublished"}
        assert [
            record for record in after_events if record.envelope.event_type in dispatch_types
        ] == [record for record in events if record.envelope.event_type in dispatch_types]
        assert not any(
            record.envelope.run_id == second_run_id
            and record.envelope.event_type in dispatch_types
            for record in after_events
        )
        after_status = json.loads(status_again)
        for collection in ("workers", "leases", "verified_declarations"):
            assert after_status[collection] == document[collection]
    except Exception as error:
        pytest.fail(f"{type(error).__name__}: {error}\n\n" + "\n\n".join(transcript), pytrace=True)
