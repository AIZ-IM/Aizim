from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from aizim.domain.serialization import JsonValue
from aizim.state import AppendEventCommand, StateService, StateServiceConfig

FIXTURE = Path(__file__).parents[1] / "fixtures" / "minimal_lean"
STATUS_KEYS = {
    "run",
    "workers",
    "leases",
    "epochs",
    "candidates",
    "verified_declarations",
    "denied_capabilities",
    "resources",
}


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "aizim", *args], check=False, capture_output=True, text=True
    )


def initialized_project(tmp_path: Path) -> Path:
    root = Path(shutil.copytree(FIXTURE, tmp_path / "lean-project"))
    assert run_cli("init", str(root)).returncode == 0
    return root


def append_status_fixtures(root: Path) -> None:
    events: tuple[tuple[str, str, dict[str, JsonValue]], ...] = (
        ("RunCreated", "run-1", {}),
        ("WorkerRegistered", "run-1", {"worker_id": "worker-1", "role": "formalizer"}),
        (
            "LeaseGranted",
            "run-1",
            {"lease_id": "lease-1", "worker_id": "worker-1", "document_id": "doc-1"},
        ),
        ("ContributionSubmitted", "run-1", {"contribution_id": "candidate-1"}),
        ("DeclarationPublished", "run-1", {"declaration_id": "declaration-1"}),
        (
            "CapabilityDenied",
            "run-1",
            {
                "reason_code": "UNKNOWN_TOKEN",
                "role": "formalizer",
                "worker_id": "worker-1",
                "operation": "state.query",
                "request_id": "request-1",
            },
        ),
        (
            "CapabilityMinted",
            "run-1",
            {
                "worker_id": "worker-1",
                "role": "formalizer",
                "operations": ["state.query"],
                "expires_at": "2026-07-22T00:00:00Z",
            },
        ),
    )
    with StateService(StateServiceConfig(root, "fixture-session")) as state:
        for event_type, run_id, payload in events:
            state.append_event(AppendEventCommand(event_type, "test", run_id, None, payload))


def wait_for(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists() and process.poll() is None


def test_status_json_has_stable_empty_shape_and_initial_epoch(tmp_path: Path) -> None:
    root = initialized_project(tmp_path)

    result = run_cli("status", "--project", str(root), "--json")
    document = json.loads(result.stdout)

    assert result.returncode == 0
    assert set(document) == STATUS_KEYS
    assert document["epochs"]["knowledge_epoch"] == 0
    assert all(document[key] == [] for key in STATUS_KEYS - {"epochs"})


def test_state_supervisor_command_is_hidden_from_public_help() -> None:
    result = run_cli("--help")

    assert result.returncode == 0
    assert "{init,doctor,status}" in result.stdout
    assert "state" not in result.stdout


def test_status_is_reconstructed_from_durable_projections_after_restart(
    tmp_path: Path,
) -> None:
    root = initialized_project(tmp_path)
    append_status_fixtures(root)

    result = run_cli("status", "--project", str(root), "--json")
    document = json.loads(result.stdout)

    assert result.returncode == 0
    assert all(document[key] for key in STATUS_KEYS - {"epochs"})
    assert document["epochs"]["knowledge_epoch"] == 0


def test_state_serve_refuses_second_owner_serves_status_and_cleans_on_sigterm() -> None:
    with TemporaryDirectory(prefix="aizim-cli-", dir="/tmp") as directory:
        root = initialized_project(Path(directory))
        command = [sys.executable, "-m", "aizim", "state", "serve", "--project", str(root)]
        first = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        socket_path = root / ".aizim" / "run" / "state.sock"
        pid_path = root / ".aizim" / "run" / "state.pid"
        try:
            wait_for(socket_path, first)
            assert pid_path.read_text().strip() == str(first.pid)
            second = subprocess.run(
                command, check=False, capture_output=True, text=True, timeout=5
            )
            assert second.returncode == 2
            status = run_cli("status", "--project", str(root), "--json")
            assert status.returncode == 0
            assert set(json.loads(status.stdout)) == STATUS_KEYS
        finally:
            first.terminate()
            first.wait(timeout=5)
        assert not socket_path.exists()
        assert not pid_path.exists()
        assert not (root / ".aizim" / "state.sqlite3-wal").exists()


def test_state_serve_reports_corrupt_state_without_a_traceback(tmp_path: Path) -> None:
    root = initialized_project(tmp_path)
    (root / ".aizim" / "state.sqlite3").write_bytes(b"not a database")

    result = run_cli("state", "serve", "--project", str(root))

    assert result.returncode == 2
    assert result.stderr == "aizim state serve: service failed\n"
