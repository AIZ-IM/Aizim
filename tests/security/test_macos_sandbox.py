from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import socket
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

import pytest

from aizim.agents.macos_sandbox import MacOSSandboxAdapter
from aizim.agents.sandbox import ProbeOperation, ProbeRequest
from aizim.agents.workspace_view import ViewSource, WorkspaceViewBuilder
from aizim.runtime.layout import ProjectLayout
from aizim.state import StateService, StateServiceConfig

FIXTURE = Path(__file__).parents[1] / "fixtures" / "attack_probe_project"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def listener(path: Path | None = None) -> socket.socket:
    family = socket.AF_INET if path is None else socket.AF_UNIX
    server = socket.socket(family)
    server.setblocking(False)
    if path is None:
        server.bind(("127.0.0.1", 0))
    else:
        server.bind(str(path))
        path.chmod(0o600)
    server.listen()
    return server


def accepted(server: socket.socket) -> bool:
    try:
        connection, _address = server.accept()
    except BlockingIOError:
        return False
    connection.close()
    return True


@pytest.mark.macos_sandbox
def test_real_macos_sandbox_denies_all_protected_surfaces() -> None:
    with TemporaryDirectory(prefix="aizim-attack-") as directory:
        root = Path(shutil.copytree(FIXTURE, Path(directory) / "project"))
        layout = ProjectLayout.from_lean_project(root)
        layout.prepare_runtime()
        allowed = root / "Allowed.lean"
        unleased = root / "Unleased.lean"
        view = WorkspaceViewBuilder().materialize(
            root,
            (
                ViewSource(
                    PurePosixPath("Allowed.lean"),
                    hashlib.sha256(allowed.read_bytes()).hexdigest(),
                ),
            ),
        )
        tcp = listener()
        gateway_path = Path("/private/tmp") / f"aizim-{view.view_root.name}.sock"
        gateway = listener(gateway_path)
        secret = "sandbox-secret-must-not-appear"
        try:
            with StateService(StateServiceConfig(root, "sandbox-test")) as state:
                shared = layout.artifact_root / "shared.txt"
                shared.write_text("shared immutable artifact\n")
                before_assets = (sha256(unleased), sha256(shared))
                before_digest = state.logical_digest()
                tcp_host, tcp_port = tcp.getsockname()
                environment = dict(os.environ, AIZIM_ATTACK_SECRET=secret)
                report = asyncio.run(
                    MacOSSandboxAdapter().launch_probe(
                        ProbeRequest(
                            probe_id="probe-1",
                            run_id="run-1",
                            project_root=root,
                            view_root=view.view_root,
                            scratch_root=view.scratch_root,
                            state_database=layout.database_path,
                            unleased_file=unleased,
                            shared_artifact=shared,
                            gateway_socket=gateway_path,
                            tcp_host=str(tcp_host),
                            tcp_port=int(tcp_port),
                            allowed_view_file=view.view_root / "Allowed.lean",
                            allowed_view_sha256=sha256(view.view_root / "Allowed.lean"),
                            secret_environment_name="AIZIM_ATTACK_SECRET",
                            parent_env=environment,
                            event_sink=state,
                            timeout_seconds=20.0,
                        )
                    )
                )
                after_assets = (sha256(unleased), sha256(shared))
                events = tuple(
                    record.envelope
                    for record in state.query_events("run-1")
                    if record.envelope.event_type.startswith("SandboxProbe")
                )
                after_digest = state.logical_digest()
                assert state.replay_verify().matched

            with StateService(StateServiceConfig(root, "sandbox-restart")) as restarted:
                assert restarted.logical_digest() == before_digest
                assert restarted.replay_verify().matched
                assert len(restarted.query_events("run-1")) == 11

            assert report.passed
            assert report.codex_version == "codex-cli 0.144.6"
            assert report.sandbox_executable == "/usr/bin/sandbox-exec"
            assert len(report.policy_hash) == 64
            assert tuple(attempt.operation for attempt in report.attempts) == tuple(ProbeOperation)
            assert all(attempt.verdict == "denied" for attempt in report.attempts[:9])
            assert all(attempt.verdict == "allowed" for attempt in report.attempts[9:])
            assert report.unexpected_allows == ()
            assert before_assets == after_assets
            assert before_digest == after_digest == report.logical_digest_before
            assert report.logical_digest_after == before_digest
            assert not accepted(tcp)
            assert not accepted(gateway)
            assert len(events) == 11
            assert [event.event_type for event in events].count("SandboxProbeDenied") == 9
            assert [event.event_type for event in events].count("SandboxProbePassed") == 2
            probe_ids: list[str] = []
            for event in events:
                probe_id = event.payload["probe_id"]
                assert isinstance(probe_id, str)
                probe_ids.append(probe_id)
                assert event.payload["policy_hash"] == report.policy_hash
            assert probe_ids == [f"probe-1:{operation.value}" for operation in ProbeOperation]
            serialized = repr(report) + repr(events)
            assert secret not in serialized
            assert str(root) not in serialized
        finally:
            tcp.close()
            gateway.close()
            gateway_path.unlink(missing_ok=True)
            view.close()
