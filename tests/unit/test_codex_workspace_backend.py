from __future__ import annotations

from pathlib import Path

import pytest

from aizim.agents import AgentRequest, AgentResult, BackendIdentity, WorkspaceViewBuilder
from aizim.domain import AgentRole
from aizim.orchestration.codex_worker import (
    CodexWorkspaceBackend,
    project_view_sources,
    proof_instruction,
)


class RecordingBackend:
    identity = BackendIdentity("codex", "codex-cli 0.144.6", "a" * 64)

    def __init__(self) -> None:
        self.request: AgentRequest | None = None
        self.source = ""

    async def run(self, request: AgentRequest) -> AgentResult:
        self.request = request
        self.source = (request.view_root / "AizimSmoke" / "Base.lean").read_text()
        return AgentResult(request.worker_id, "submitted", "aligned", "b" * 64, "c" * 64, 0)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "AizimSmoke").mkdir(parents=True)
    (project / "lakefile.toml").write_text('name = "Smoke"\n')
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
    (project / "AizimSmoke" / "Base.lean").write_text("import Std\n")
    (project / ".aizim").mkdir()
    (project / ".aizim" / "ignored.lean").write_text("import Std\n")
    return project


def _request(project: Path) -> AgentRequest:
    return AgentRequest(
        "shared-test",
        "prover-a",
        AgentRole.PROOF_EXPLORER,
        "base prompt",
        None,
        project / ".aizim" / "placeholder-view",
        project / ".aizim" / "placeholder-scratch",
        "session-test",
        project / ".aizim" / "run" / "gateway.sock",
        30.0,
        {"document_id": "document-test"},
    )


def test_project_view_sources_exclude_runtime_state(tmp_path: Path) -> None:
    project = _project(tmp_path)

    paths = tuple(item.relative_path.as_posix() for item in project_view_sources(project))

    assert paths == ("AizimSmoke/Base.lean", "lakefile.toml", "lean-toolchain")


@pytest.mark.asyncio
async def test_codex_workspace_backend_replaces_canonical_view_and_cleans_it(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    backend = RecordingBackend()
    wrapper = CodexWorkspaceBackend(backend, project, "model-fixture", "use gateway only")

    result = await wrapper.run(_request(project))

    request = backend.request
    assert result.status == "submitted"
    assert request is not None
    assert request.model == "model-fixture"
    assert request.view_root.parent != project
    assert request.gateway_broker_socket == project / ".aizim" / "run" / "gateway.sock"
    assert request.prompt.endswith("use gateway only")
    assert backend.source == "import Std\n"
    assert not request.view_root.exists()
    assert not request.scratch_root.exists()


@pytest.mark.asyncio
async def test_codex_workspace_backend_cleans_materialization_when_instruction_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    materialized: list[Path] = []

    original = WorkspaceViewBuilder.materialize

    def capture(self, project_root, sources):
        workspace = original(self, project_root, sources)
        materialized.extend((workspace.view_root, workspace.scratch_root))
        return workspace

    monkeypatch.setattr(WorkspaceViewBuilder, "materialize", capture)

    def fail(_request: AgentRequest) -> str:
        raise RuntimeError("instruction failed")

    wrapper = CodexWorkspaceBackend(RecordingBackend(), project, "model-fixture", fail)

    with pytest.raises(RuntimeError, match="instruction failed"):
        await wrapper.run(_request(project))

    assert materialized
    assert all(not path.exists() for path in materialized)


def test_second_round_instruction_binds_only_verified_delta() -> None:
    instruction = proof_instruction(
        "prover-b",
        1,
        "document-test",
        {"fully_qualified_name": "AizimSmoke.Research.a_add_zero", "module": "Generated"},
        "shared-test",
        0,
    )

    assert "AizimSmoke.Research.a_add_zero" in instruction
    assert "import_module=Generated" in instruction
    assert "document-test" in instruction
