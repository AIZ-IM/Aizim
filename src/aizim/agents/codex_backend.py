from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from aizim.async_lifecycle import await_cleanup
from aizim.config import CODEX_CLI_VERSION
from aizim.domain import sha256_file
from aizim.runtime.provider_executables import ResolvedExecutable

from .backend import AgentRequest, AgentResult, BackendIdentity
from .launcher import CodexLaunchOutcome, CodexLaunchSpec
from .sandbox import SandboxLaunchSpec, validate_launch_spec

_CODEX_VERSION = f"codex-cli {CODEX_CLI_VERSION}"
type SandboxCompiler = Callable[[AgentRequest], SandboxLaunchSpec]
type CodexVersion = Callable[[Path], str]
type CodexLauncher = Callable[[CodexLaunchSpec], Awaitable[CodexLaunchOutcome]]
type AgentFinalizer = Callable[[AgentRequest], Awaitable[None]]


@dataclass(slots=True)
class CodexBackendError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class CodexBackendDependencies:
    codex_executable: ResolvedExecutable
    codex_version: CodexVersion = field(repr=False)
    sandbox: SandboxCompiler = field(repr=False)
    sidecar_executable: Path
    launch: CodexLauncher = field(repr=False)
    revoke: AgentFinalizer = field(repr=False)
    cleanup: AgentFinalizer = field(repr=False)


class CodexBackend:
    def __init__(self, dependencies: CodexBackendDependencies) -> None:
        descriptor = dependencies.codex_executable
        try:
            executable = descriptor.path.resolve(strict=True)
            version = dependencies.codex_version(executable)
            digest = sha256_file(executable)
        except OSError as error:
            raise CodexBackendError("CODEX_EXECUTABLE_UNAVAILABLE") from error
        if not executable.is_file() or executable != descriptor.path:
            raise CodexBackendError("CODEX_EXECUTABLE_UNAVAILABLE")
        if version != _CODEX_VERSION or version != descriptor.version:
            raise CodexBackendError("UNSUPPORTED_CODEX_VERSION")
        if digest != descriptor.sha256:
            raise CodexBackendError("CODEX_IMAGE_CHANGED")
        if not dependencies.sidecar_executable.is_absolute():
            raise CodexBackendError("SIDECAR_EXECUTABLE_INVALID")
        self._dependencies = dependencies
        self._executable = executable
        self._image_hash = descriptor.sha256
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
            current_version = self._dependencies.codex_version(current_path)
            current_hash = sha256_file(current_path)
        except (OSError, RuntimeError) as error:
            raise CodexBackendError("CODEX_IMAGE_CHANGED") from error
        if (
            current_path != self._executable
            or current_version != self._identity.version
            or current_hash != self._image_hash
        ):
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
        )
        outcome = await self._dependencies.launch(spec)
        return AgentResult(
            request.worker_id,
            outcome.status,
            outcome.summary,
            outcome.transport_event_hash,
            outcome.final_message_hash,
            outcome.exit_code,
            sandbox.policy_hash,
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
) -> CodexLaunchSpec:
    if (
        sandbox.cwd != request.view_root
        or sandbox.view_root != request.view_root
        or sandbox.scratch_root != request.scratch_root
        or not sidecar_executable.is_absolute()
    ):
        raise CodexBackendError("SANDBOX_SPEC_MISMATCH")
    try:
        _project_root(request)
        validate_launch_spec(request, sandbox)
    except (OSError, ValueError) as error:
        raise CodexBackendError("SANDBOX_SPEC_INVALID") from error
    sandbox_command = 9
    schema_name = (
        "codex_alignment_result.schema.json"
        if request.result_schema == "alignment"
        else "codex_result.schema.json"
    )
    schema_path = Path(__file__).with_name(schema_name).resolve()
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
    return socket_path.parents[2].resolve(strict=True)


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
        'default_tools_approval_mode="approve"',
    )
    return "mcp_servers.aizim={" + ",".join(values) + "}"
