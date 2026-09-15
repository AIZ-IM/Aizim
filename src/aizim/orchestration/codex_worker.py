from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath

from aizim.agents import (
    AgentBackend,
    AgentRequest,
    AgentResult,
    BackendIdentity,
    CodexBackend,
    CodexBackendDependencies,
    ViewSource,
    WorkspaceViewBuilder,
)
from aizim.agents.launcher import launch_codex
from aizim.agents.macos_sandbox import SandboxHostError
from aizim.agents.platform_sandbox import sandbox_adapter
from aizim.agents.sandbox import SandboxRequest
from aizim.domain import sha256_file
from aizim.lean.document_io import ensure_tree, open_root
from aizim.lean.source_layout import lakefile
from aizim.runtime.distribution import without_distribution_environment
from aizim.runtime.provider_executables import (
    ProviderExecutableError,
    ResolvedExecutable,
    codex_runtime_root,
    revalidate_codex,
)

from .controller_process import private_workspace
from .run_identity import candidate_name, contribution_id


class CodexWorkerError(RuntimeError):
    pass


type Instruction = str | Callable[[AgentRequest], str]


class CodexWorkspaceBackend:
    def __init__(
        self, backend: AgentBackend, project_root: Path, model: str, instruction: Instruction
    ) -> None:
        if (
            not isinstance(project_root, Path)
            or type(model) is not str
            or not model
            or (not isinstance(instruction, str) and not callable(instruction))
        ):
            raise CodexWorkerError("INVALID_CODEX_WORKER")
        self._backend = backend
        self._project_root, self._model, self._instruction = project_root, model, instruction

    @property
    def identity(self) -> BackendIdentity:
        return self._backend.identity

    async def run(self, request: AgentRequest) -> AgentResult:
        workspace = WorkspaceViewBuilder().materialize(
            self._project_root, project_view_sources(self._project_root)
        )
        try:
            source = self._instruction
            instruction = source if isinstance(source, str) else source(request)
            if type(instruction) is not str or not instruction:
                raise CodexWorkerError("INVALID_CODEX_WORKER_INSTRUCTION")
            root_fd = open_root(self._project_root)
            try:
                output_fd = ensure_tree(root_fd, (".aizim", "run"))
                os.close(output_fd)
            finally:
                os.close(root_fd)
            with tempfile.TemporaryDirectory(
                prefix="aizim-result-", dir=self._project_root / ".aizim/run"
            ) as output:
                isolated = replace(
                    request,
                    prompt=f"{request.prompt}\n{instruction}",
                    model=self._model,
                    view_root=workspace.view_root,
                    scratch_root=workspace.scratch_root,
                    result_root=Path(output),
                )
                return await self._backend.run(isolated)
        finally:
            workspace.close()


def create_codex_backend(
    executable: ResolvedExecutable,
    parent_environment: Mapping[str, str] | None = None,
) -> CodexBackend:
    source_environment = dict(os.environ if parent_environment is None else parent_environment)
    try:
        revalidate_codex(executable, source_environment)
    except ProviderExecutableError as error:
        raise CodexWorkerError(error.code) from error
    path = executable.path
    sidecar = _sidecar_executable()
    environment = without_distribution_environment(source_environment)
    sandbox = sandbox_adapter(path)
    runtime_read_roots = _runtime_read_roots(path)

    def codex_version(_path: Path) -> str:
        try:
            revalidate_codex(executable, source_environment)
        except ProviderExecutableError as error:
            raise CodexWorkerError(error.code) from error
        return executable.version

    def compile_sandbox(request: AgentRequest):
        return sandbox.compile(
            SandboxRequest(
                _canonical_project(request),
                request.view_root,
                request.scratch_root,
                (str(path),),
                environment,
                runtime_read_roots,
            )
        )

    return CodexBackend(
        CodexBackendDependencies(
            executable,
            codex_version,
            compile_sandbox,
            sidecar,
            launch_codex,
            _noop,
            _noop,
        )
    )


async def preflight_codex_worker(
    project_root: Path,
    executable: ResolvedExecutable,
    parent_environment: Mapping[str, str] | None = None,
) -> None:
    source = dict(os.environ if parent_environment is None else parent_environment)
    try:
        revalidate_codex(executable, source)
    except ProviderExecutableError as error:
        raise CodexWorkerError(error.code) from error
    path = executable.path
    with private_workspace("aizim-worker-") as (_private, view, scratch):
        request = SandboxRequest(
            project_root,
            view,
            scratch,
            (str(path),),
            without_distribution_environment(source),
            _runtime_read_roots(path),
        )
        try:
            sandbox_adapter(path).compile(request)
        except (SandboxHostError, ValueError) as error:
            raise CodexWorkerError("WORKER_SANDBOX_UNAVAILABLE") from error
        try:
            revalidate_codex(executable, source)
        except ProviderExecutableError as error:
            raise CodexWorkerError(error.code) from error


def _runtime_read_roots(executable: Path) -> tuple[Path, ...]:
    return (codex_runtime_root(executable),)


def project_view_sources(project_root: Path) -> tuple[ViewSource, ...]:
    try:
        root = project_root.resolve(strict=True)
    except OSError as error:
        raise CodexWorkerError("PROJECT_VIEW_UNAVAILABLE") from error
    controls = tuple(path for path in (lakefile(root), root / "lean-toolchain"))
    if not root.is_dir() or any(not item.is_file() for item in controls):
        raise CodexWorkerError("PROJECT_VIEW_UNAVAILABLE")
    files = [*controls]
    files.extend(
        item
        for item in root.rglob("*.lean")
        if item not in controls
        and item.is_file()
        and all(not part.startswith(".") for part in item.relative_to(root).parts)
    )
    return tuple(
        ViewSource(PurePosixPath(item.relative_to(root).as_posix()), sha256_file(item))
        for item in sorted(files, key=lambda item: item.relative_to(root).as_posix())
    )


def proof_instruction(
    worker_id: str,
    round_index: int,
    document_id: str,
    known_delta: Mapping[str, str] | None,
    run_id: str,
    knowledge_epoch: int,
) -> str:
    if worker_id == "prover-a" and round_index == 0:
        return _first_worker_instruction(document_id, run_id, knowledge_epoch)
    if worker_id == "prover-b" and round_index == 0:
        return _waiting_worker_instruction(document_id, knowledge_epoch)
    if worker_id == "prover-b" and round_index == 1 and known_delta is not None:
        return _second_worker_instruction(document_id, known_delta, run_id)
    raise CodexWorkerError("UNSUPPORTED_CODEX_WORKER_ROUND")


def codex_worker_factory(backend: AgentBackend, project_root: Path, model: str):
    def factory(worker_id: str, round_index: int, delta: dict[str, str] | None):
        def instruction(request: AgentRequest) -> str:
            document_id = request.context.get("document_id")
            knowledge_epoch = request.context.get("knowledge_epoch")
            if (
                type(document_id) is not str
                or not document_id
                or type(knowledge_epoch) is not int
                or knowledge_epoch < 0
            ):
                raise CodexWorkerError("DOCUMENT_CONTEXT_UNAVAILABLE")
            return proof_instruction(
                worker_id,
                round_index,
                document_id,
                delta,
                request.run_id,
                knowledge_epoch,
            )

        return CodexWorkspaceBackend(backend, project_root, model, instruction)

    return factory


def _first_worker_instruction(document_id: str, run_id: str, knowledge_epoch: int) -> str:
    contribution = contribution_id(run_id, "prover-a")
    candidate = candidate_name(run_id, "prover-a")
    return (
        "Use only the gateway calls below, each with one payload object. The document is "
        f"{document_id}. First call lean.goal at line 4. Then call lean.multi_attempt at line 4 "
        'with snippets ["rfl", "exact Nat.add_zero n"]. Only if its diagnostics are empty, call '
        "document.apply with accepted=exact Nat.add_zero n. Then call contribution.submit with "
        f"contribution_id={contribution}, candidate_name={candidate}, "
        "complete_type=(n : Nat) : n + 0 = n, imports=[Std], and empty dependencies, "
        "assumptions, and evidence_links. Finally call knowledge.read "
        f"after_knowledge_epoch={knowledge_epoch} "
        "with wait=true. Do not write files or use unlisted tools."
    )


def _waiting_worker_instruction(document_id: str, knowledge_epoch: int) -> str:
    return (
        "Use only the gateway calls below, each with one payload object. The document is "
        f"{document_id}. Call lean.goal at line 4, then call knowledge.read with "
        f"after_knowledge_epoch={knowledge_epoch} and wait=true. This round must not edit or "
        "submit a document; "
        "the next round receives the verified delta. Do not write files or use unlisted tools."
    )


def _second_worker_instruction(document_id: str, delta: Mapping[str, str], run_id: str) -> str:
    name, module = _delta_text(delta, "fully_qualified_name"), _delta_text(delta, "module")
    contribution = contribution_id(run_id, "prover-b")
    candidate = candidate_name(run_id, "prover-b")
    return (
        "Use only the gateway calls below, each with one payload object. The verified declaration "
        f"is {name} in module {module}. The document is {document_id}. Call document.apply with "
        f"accepted=exact {name} n and import_module={module}. Then call contribution.submit with "
        f"contribution_id={contribution}, candidate_name={candidate}, "
        "complete_type=(n : Nat) : n + 0 = n, imports=[Std], and empty dependencies, "
        "assumptions, and evidence_links. Do not write files or use unlisted tools."
    )


def _sidecar_executable() -> Path:
    try:
        sidecar = Path(sys.executable).with_name("aizim-gateway-sidecar").resolve(strict=True)
    except OSError as error:
        raise CodexWorkerError("SIDECAR_EXECUTABLE_UNAVAILABLE") from error
    if not sidecar.is_file() or not os.access(sidecar, os.X_OK):
        raise CodexWorkerError("SIDECAR_EXECUTABLE_UNAVAILABLE")
    return sidecar


def _canonical_project(request: AgentRequest) -> Path:
    socket = request.gateway_broker_socket
    if socket.name != "gateway.sock" or tuple(part.name for part in socket.parents[:2]) != (
        "run",
        ".aizim",
    ):
        raise CodexWorkerError("GATEWAY_SOCKET_NONCANONICAL")
    try:
        return socket.parents[2].resolve(strict=True)
    except OSError as error:
        raise CodexWorkerError("GATEWAY_SOCKET_NONCANONICAL") from error


def _delta_text(delta: Mapping[str, str], field: str) -> str:
    value = delta.get(field)
    if type(value) is not str or not value:
        raise CodexWorkerError("INVALID_VERIFIED_DELTA")
    return value


async def _noop(_request: AgentRequest) -> None:
    return None
