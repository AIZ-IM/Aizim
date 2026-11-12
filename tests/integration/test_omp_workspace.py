from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from aizim.agents.backend import AgentRequest, AgentResult, BackendIdentity
from aizim.agents.workspace_view import WorkspaceView, WorkspaceViewBuilder
from aizim.domain import AgentRole, sha256_file
from aizim.orchestration.agent_workspace import (
    ResearchWorkspaceBackend,
    project_view_sources,
)


@pytest.fixture
def private_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    temporary = tmp_path / "private-tmp"
    temporary.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    return temporary


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "Research").mkdir(parents=True)
    (root / "lakefile.toml").write_text('name = "Research"\n')
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
    (root / "Research/Base.lean").write_text("import Std\ntheorem base : True := by trivial\n")
    (root / "Research/Target.lean").write_text("import Research.Base\n")
    (root / ".aizim/run").mkdir(parents=True)
    (root / ".aizim/private.lean").write_text("private state\n")
    (root / ".git").mkdir()
    (root / ".git/private.lean").write_text("private repository state\n")
    (root / "notes.txt").write_text("operator notes\n")
    return root


def request_for(project: Path) -> AgentRequest:
    return AgentRequest(
        run_id="research-run", worker_id="prover-test", role=AgentRole.PROOF_EXPLORER,
        prompt="Prove the immutable target.", model=None,
        view_root=project / ".aizim/old-view", scratch_root=project / ".aizim/old-scratch",
        gateway_session_id="parent-session",
        gateway_broker_socket=project / ".aizim/run/gateway.sock",
        timeout_seconds=30, context={"target_id": "target-test"},
        result_root=project / "old-results",
    )


class InspectingBackend:
    identity = BackendIdentity("omp", "omp test", "a" * 64)

    def __init__(self, project: Path, outcome: str = "complete") -> None:
        self.project = project
        self.outcome = outcome
        self.request: AgentRequest | None = None
        self.observed_hashes: dict[str, str] = {}
        self.started = asyncio.Event()
        self.exited = False
        self.result: AgentResult | None = None

    async def run(self, request: AgentRequest) -> AgentResult:
        self.request = request
        assert request.model is None
        assert request.result_root is None
        assert request.view_root.is_dir()
        assert request.scratch_root.is_dir()
        assert not request.view_root.is_relative_to(self.project)
        assert not request.scratch_root.is_relative_to(self.project)
        assert request.view_root != request.scratch_root
        assert request.view_root.parent == request.scratch_root.parent
        assert request.view_root.parent.stat().st_mode & 0o777 == 0o700
        expected = {"Research/Base.lean", "Research/Target.lean", "lakefile.toml", "lean-toolchain"}
        actual = {
            path.relative_to(request.view_root).as_posix()
            for path in request.view_root.rglob("*") if path.is_file()
        }
        assert actual == expected
        for relative in expected:
            copied = request.view_root / relative
            assert copied.read_bytes() == (self.project / relative).read_bytes()
            assert copied.stat().st_mode & 0o222 == 0
            self.observed_hashes[relative] = sha256_file(copied)
        (request.scratch_root / "attempt.log").write_text("ephemeral attempt data\n")
        self.started.set()
        try:
            if self.outcome == "cancel":
                await asyncio.Event().wait()
            if self.outcome == "fail":
                raise RuntimeError("backend attempt failed")
            self.result = AgentResult(
                request.worker_id, "abstained", "No trusted contribution submitted.",
                "b" * 64, "c" * 64, 0, model="magpie/research",
            )
            return self.result
        finally:
            self.exited = True


def test_project_view_sources_are_hashed_and_exclude_private_state(project: Path) -> None:
    sources = project_view_sources(project)

    assert [source.relative_path.as_posix() for source in sources] == [
        "Research/Base.lean", "Research/Target.lean", "lakefile.toml", "lean-toolchain",
    ]
    for source in sources:
        assert source.sha256 == sha256_file(project / source.relative_path)


@pytest.mark.asyncio
async def test_workspace_materializes_hashed_sources_and_cleans_on_completion(
    project: Path, private_tmpdir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = InspectingBackend(project)
    request = request_for(project)
    workspaces: list[WorkspaceView] = []
    original = WorkspaceViewBuilder.materialize

    def capture(self, project_root, sources):
        workspace = original(self, project_root, sources)
        workspaces.append(workspace)
        return workspace

    monkeypatch.setattr(WorkspaceViewBuilder, "materialize", capture)
    instruction_requests: list[AgentRequest] = []

    def instruction(incoming: AgentRequest) -> str:
        instruction_requests.append(incoming)
        return "Use only the trusted gateway host tools."

    wrapper = ResearchWorkspaceBackend(backend, project, instruction)

    result = await wrapper.run(request)

    assert result is backend.result
    assert wrapper.identity == backend.identity
    assert instruction_requests == [request]
    assert backend.request is not None
    actual = backend.request
    assert actual.prompt == request.prompt + "\nUse only the trusted gateway host tools."
    assert actual.run_id == request.run_id
    assert actual.worker_id == request.worker_id
    assert actual.role == request.role
    assert actual.gateway_session_id == request.gateway_session_id
    assert actual.gateway_broker_socket == request.gateway_broker_socket
    assert actual.timeout_seconds == request.timeout_seconds
    assert actual.context == request.context
    parent = actual.view_root.parent
    assert actual.scratch_root.parent == parent
    assert parent.parent == private_tmpdir
    assert request.model is None
    workspace, = workspaces
    assert {
        entry.relative_path: entry.sha256 for entry in workspace.entries
    } == backend.observed_hashes
    manifest = json.dumps([
        {"byte_length": entry.byte_length, "mode": entry.mode,
         "relative_path": entry.relative_path, "sha256": entry.sha256}
        for entry in workspace.entries
    ], sort_keys=True, separators=(",", ":")).encode()
    assert workspace.manifest_sha256 == hashlib.sha256(manifest).hexdigest()
    assert backend.exited
    assert not actual.view_root.exists()
    assert not actual.scratch_root.exists()
    assert not parent.exists()
    assert (project / "Research/Base.lean").read_text() == (
        "import Std\ntheorem base : True := by trivial\n"
    )


@pytest.mark.asyncio
async def test_workspace_closes_on_backend_failure(project: Path, private_tmpdir: Path) -> None:
    backend = InspectingBackend(project, "fail")
    wrapper = ResearchWorkspaceBackend(backend, project, "Use the trusted gateway.")

    with pytest.raises(RuntimeError, match="backend attempt failed"):
        await wrapper.run(request_for(project))

    assert backend.exited
    assert backend.request is not None
    assert not backend.request.view_root.exists()
    assert not backend.request.scratch_root.exists()
    assert list(private_tmpdir.iterdir()) == []


@pytest.mark.asyncio
async def test_workspace_closes_after_backend_cancellation(
    project: Path, private_tmpdir: Path,
) -> None:
    backend = InspectingBackend(project, "cancel")
    wrapper = ResearchWorkspaceBackend(backend, project, "Use the trusted gateway.")
    task = asyncio.create_task(wrapper.run(request_for(project)))
    try:
        await asyncio.wait_for(backend.started.wait(), timeout=2)
        assert backend.request is not None
        assert backend.request.view_root.exists()
        assert backend.request.scratch_root.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert backend.exited
    assert backend.request is not None
    assert not backend.request.view_root.exists()
    assert not backend.request.scratch_root.exists()
    assert list(private_tmpdir.iterdir()) == []


@pytest.mark.asyncio
async def test_workspace_closes_if_instruction_generation_fails(
    project: Path, private_tmpdir: Path,
) -> None:
    backend = InspectingBackend(project)

    def fail(_request: AgentRequest) -> str:
        raise ValueError("instruction unavailable")

    wrapper = ResearchWorkspaceBackend(backend, project, fail)
    with pytest.raises(ValueError, match="instruction unavailable"):
        await wrapper.run(request_for(project))

    assert backend.request is None
    assert list(private_tmpdir.iterdir()) == []
