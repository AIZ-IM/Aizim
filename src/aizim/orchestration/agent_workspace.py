from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path, PurePosixPath

from aizim.agents.backend import AgentBackend, AgentRequest, AgentResult, BackendIdentity
from aizim.agents.workspace_view import ViewSource, WorkspaceViewBuilder
from aizim.domain import sha256_file
from aizim.lean.source_layout import lakefile


class ResearchWorkspaceError(RuntimeError):
    pass


def project_view_sources(project_root: Path) -> tuple[ViewSource, ...]:
    try:
        root = project_root.resolve(strict=True)
        controls = (lakefile(root), root / "lean-toolchain")
        if not root.is_dir() or any(not item.is_file() for item in controls):
            raise OSError
        files = [*controls]
        files.extend(
            item for item in root.rglob("*.lean")
            if item not in controls and item.is_file()
            and all(not part.startswith(".") for part in item.relative_to(root).parts)
        )
        return tuple(
            ViewSource(PurePosixPath(item.relative_to(root).as_posix()), sha256_file(item))
            for item in sorted(files, key=lambda item: item.relative_to(root).as_posix())
        )
    except OSError as error:
        raise ResearchWorkspaceError("PROJECT_VIEW_UNAVAILABLE") from error


class ResearchWorkspaceBackend:
    def __init__(self, backend: AgentBackend, project_root: Path,
                 instruction: str | Callable[[AgentRequest], str]) -> None:
        if not isinstance(project_root, Path) or not (
            isinstance(instruction, str) or callable(instruction)
        ):
            raise ResearchWorkspaceError("INVALID_RESEARCH_WORKSPACE")
        self._backend = backend
        self._project_root = project_root
        self._instruction = instruction

    @property
    def identity(self) -> BackendIdentity:
        return self._backend.identity

    async def run(self, request: AgentRequest) -> AgentResult:
        if request.model is not None:
            raise ResearchWorkspaceError("OMP_MODEL_OVERRIDE_FORBIDDEN")
        workspace = WorkspaceViewBuilder().materialize(
            self._project_root, project_view_sources(self._project_root)
        )
        try:
            instruction = self._instruction
            text = instruction(request) if callable(instruction) else instruction
            if type(text) is not str or not text:
                raise ResearchWorkspaceError("INVALID_RESEARCH_INSTRUCTION")
            return await self._backend.run(replace(
                request, prompt=f"{request.prompt}\n{text}",
                view_root=workspace.view_root, scratch_root=workspace.scratch_root,
                result_root=None,
            ))
        finally:
            workspace.close()
