from __future__ import annotations

import asyncio  # noqa: ANYIO_OK - exercises the asyncio Linux sandbox adapter
import hashlib
import os
import shutil
import socket
import sys
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

import pytest

from aizim.agents.linux_sandbox import LinuxSandboxAdapter
from aizim.agents.sandbox import ProbeOperation, ProbeRequest
from aizim.agents.workspace_view import ViewSource, WorkspaceViewBuilder
from aizim.runtime.layout import ProjectLayout
from aizim.runtime.provider_executables import codex_runtime_root, resolve_codex
from aizim.state import StateService, StateServiceConfig

FIXTURE = Path(__file__).parents[1] / "fixtures" / "attack_probe_project"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def listener(path: Path | None = None) -> socket.socket:
    family = socket.AF_INET if path is None else socket.AF_UNIX
    server = socket.socket(family)
    server.setblocking(False)
    server.bind(("127.0.0.1", 0) if path is None else str(path))
    server.listen()
    return server


def accepted(server: socket.socket) -> bool:
    try:
        connection, _address = server.accept()
    except BlockingIOError:
        return False
    connection.close()
    return True


@pytest.mark.linux_sandbox
@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux")
def test_real_linux_sandbox_denies_all_protected_surfaces() -> None:
    with TemporaryDirectory(prefix="aizim-linux-attack-") as directory:
        private_root = Path(directory)
        root = Path(shutil.copytree(FIXTURE, private_root / "project"))
        layout = ProjectLayout.from_lean_project(root)
        layout.prepare_runtime()
        allowed = root / "Allowed.lean"
        unleased = root / "Unleased.lean"
        view = WorkspaceViewBuilder().materialize(
            root,
            (
                ViewSource(
                    PurePosixPath("Allowed.lean"),
                    sha256(allowed),
                ),
            ),
        )
        tcp = listener()
        gateway_path = private_root / "gateway.sock"
        gateway = listener(gateway_path)
        secret = "linux-sandbox-secret-must-not-appear"
        codex = resolve_codex(os.environ).path
        try:
            with StateService(StateServiceConfig(root, "linux-sandbox-test")) as state:
                shared = layout.artifact_root / "shared.txt"
                shared.write_text("shared immutable artifact\n")
                before_assets = (sha256(unleased), sha256(shared))
                before_digest = state.logical_digest()
                tcp_host, tcp_port = tcp.getsockname()
                report = asyncio.run(
                    LinuxSandboxAdapter.for_executable(codex).launch_probe(
                        ProbeRequest(
                            probe_id="probe-linux-1",
                            run_id="run-linux-1",
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
                            parent_env=dict(os.environ, AIZIM_ATTACK_SECRET=secret),
                            event_sink=state,
                            timeout_seconds=20.0,
                            runtime_read_roots=(codex_runtime_root(codex),),
                        )
                    )
                )
                after_assets = (sha256(unleased), sha256(shared))
                events = tuple(
                    record.envelope
                    for record in state.query_events("run-linux-1")
                    if record.envelope.event_type.startswith("SandboxProbe")
                )
                after_digest = state.logical_digest()
                assert state.replay_verify().matched

            with StateService(StateServiceConfig(root, "linux-sandbox-restart")) as state:
                assert state.logical_digest() == before_digest
                assert state.replay_verify().matched
                assert len(state.query_events("run-linux-1")) == 11

            assert report.passed
            assert report.platform_id == "linux"
            assert report.codex_version == "codex-cli 0.154.0"
            assert report.sandbox_executable.endswith("/codex-resources/bwrap")
            assert tuple(attempt.operation for attempt in report.attempts) == tuple(
                ProbeOperation
            )
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
            serialized = repr(report) + repr(events)
            assert secret not in serialized
            assert str(root) not in serialized
        finally:
            tcp.close()
            gateway.close()
            gateway_path.unlink(missing_ok=True)
            view.close()
