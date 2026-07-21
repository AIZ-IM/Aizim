from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from aizim.async_lifecycle import await_cleanup
from aizim.domain import sha256_file

from .backend import AgentRequest, AgentResult, BackendIdentity
from .launcher import CodexLaunchOutcome, CodexLaunchSpec
from .macos_profile import validate_macos_profile
from .sandbox import SandboxLaunchSpec

_CODEX_VERSION = "codex-cli 0.144.6"
type SandboxCompiler = Callable[[AgentRequest], SandboxLaunchSpec]
type CodexVersion = Callable[[Path], str]
type CodexLauncher = Callable[[CodexLaunchSpec], Awaitable[CodexLaunchOutcome]]
type AgentFinalizer = Callable[[AgentRequest], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CodexBackendError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class CodexBackendDependencies:
    codex_executable: Path
    codex_version: CodexVersion = field(repr=False)
    sandbox: SandboxCompiler = field(repr=False)
    developer_root: Path
    sidecar_executable: Path
    launch: CodexLauncher = field(repr=False)
    revoke: AgentFinalizer = field(repr=False)
    cleanup: AgentFinalizer = field(repr=False)


class CodexBackend:
    def __init__(self, dependencies: CodexBackendDependencies) -> None:
        try:
            executable = dependencies.codex_executable.resolve(strict=True)
            version = dependencies.codex_version(executable)
        except OSError as error:
            raise CodexBackendError("CODEX_EXECUTABLE_UNAVAILABLE") from error
        if not executable.is_file() or version != _CODEX_VERSION:
            raise CodexBackendError("UNSUPPORTED_CODEX_VERSION")
        if not dependencies.sidecar_executable.is_absolute():
            raise CodexBackendError("SIDECAR_EXECUTABLE_INVALID")
        if (
            not dependencies.developer_root.is_absolute()
            or dependencies.developer_root != dependencies.developer_root.resolve()
        ):
            raise CodexBackendError("DEVELOPER_ROOT_INVALID")
        self._dependencies = dependencies
        self._executable = executable
        self._image_hash = sha256_file(executable)
        self._identity = BackendIdentity("codex", version, self._image_hash)

    @property
    def identity(self) -> BackendIdentity:
        return self._identity

    async def run(self, request: AgentRequest) -> AgentResult:
        try:
            result = await self._run(request)
        except BaseException as failure:
            await self._finish(request, failure)
            raise
        await self._finish(request, None)
        return result

    async def _run(self, request: AgentRequest) -> AgentResult:
        try:
            current_path = self._executable.resolve(strict=True)
            current_hash = sha256_file(current_path)
        except OSError as error:
            raise CodexBackendError("CODEX_IMAGE_CHANGED") from error
        if current_path != self._executable or current_hash != self._image_hash:
            raise CodexBackendError("CODEX_IMAGE_CHANGED")
        sandbox = self._dependencies.sandbox(request)
        try:
            launch_path = Path(sandbox.argv[0]).resolve(strict=True)
            launch_hash = sha256_file(launch_path)
        except OSError as error:
            raise CodexBackendError("CODEX_IMAGE_CHANGED") from error
        if launch_path != self._executable or launch_hash != self._image_hash:
            raise CodexBackendError("CODEX_EXECUTABLE_MISMATCH")
        spec = build_codex_launch_spec(
            request,
            sandbox,
            self._dependencies.sidecar_executable,
            self._dependencies.developer_root,
        )
        outcome = await self._dependencies.launch(spec)
        return AgentResult(
            request.worker_id,
            outcome.status,
            outcome.summary,
            outcome.transport_event_hash,
            outcome.final_message_hash,
            outcome.exit_code,
        )

    async def _finish(self, request: AgentRequest, failure: BaseException | None) -> None:
        cleanup = asyncio.create_task(self._finalize(request))
        try:
            interruption = await await_cleanup(cleanup)
        except BaseException as cleanup_error:
            if failure is not None:
                raise cleanup_error from failure
            raise
        if failure is None and interruption is not None:
            raise interruption

    async def _finalize(self, request: AgentRequest) -> None:
        try:
            await self._dependencies.revoke(request)
        finally:
            await self._dependencies.cleanup(request)


def build_codex_launch_spec(
    request: AgentRequest,
    sandbox: SandboxLaunchSpec,
    sidecar_executable: Path,
    developer_root: Path,
) -> CodexLaunchSpec:
    if (
        sandbox.cwd != request.view_root
        or sandbox.view_root != request.view_root
        or sandbox.scratch_root != request.scratch_root
        or not sidecar_executable.is_absolute()
    ):
        raise CodexBackendError("SANDBOX_SPEC_MISMATCH")
    try:
        project_root = _project_root(request)
        validate_macos_profile(sandbox, project_root, developer_root)
    except (OSError, ValueError) as error:
        raise CodexBackendError("SANDBOX_SPEC_INVALID") from error
    sandbox_command = 9
    schema_path = Path(__file__).with_name("codex_result.schema.json").resolve()
    final_path = request.view_root / ".aizim-codex-last-message.json"
    mcp_override = _mcp_override(request, sidecar_executable)
    model = () if request.model is None else ("--model", request.model)
    argv = (
        *sandbox.argv[:sandbox_command],
        "-c",
        mcp_override,
        "--strict-config",
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--json",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(final_path),
        "-C",
        str(request.view_root),
        *model,
        "-",
    )
    return CodexLaunchSpec(
        argv,
        request.view_root,
        sandbox.parent_env,
        sandbox.shell_env,
        request.prompt.encode(),
        request.timeout_seconds,
        final_path,
        schema_path,
    )


def _project_root(request: AgentRequest) -> Path:
    socket_path = request.gateway_broker_socket
    if (
        not socket_path.is_absolute()
        or socket_path.name != "gateway.sock"
        or socket_path.parent.name != "run"
        or socket_path.parent.parent.name != ".aizim"
    ):
        raise ValueError("gateway socket does not identify the canonical project")
    project_root = socket_path.parents[2]
    resolved = project_root.resolve(strict=True)
    if resolved != project_root:
        raise ValueError("canonical project root is not resolved")
    return resolved


def _mcp_override(request: AgentRequest, sidecar_executable: Path) -> str:
    values = (
        f"command={json.dumps(str(sidecar_executable))}",
        "args=["
        + ",".join(
            json.dumps(value)
            for value in (
                "--broker-socket",
                str(request.gateway_broker_socket),
                "--session-id",
                request.gateway_session_id,
            )
        )
        + "]",
        "startup_timeout_sec=10",
        "tool_timeout_sec=60",
        "required=true",
    )
    return "mcp_servers.aizim={" + ",".join(values) + "}"
