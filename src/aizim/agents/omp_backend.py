from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aizim.async_lifecycle import await_cleanup
from aizim.gateway.transport import connect_gateway
from aizim.omp.executable import resolve_omp, revalidate_omp
from aizim.omp.rpc import RpcClient
from aizim.omp.runtime import check_magpie, stage_runtime
from aizim.omp.tools import GatewayHostTools
from aizim.runtime.executable import ResolvedExecutable

from .backend import AgentRequest, AgentResult, BackendIdentity
from .platform_sandbox import sandbox_adapter
from .sandbox import SandboxLaunchSpec, SandboxRequest, validate_launch_spec

_RESTRICTED = (
    "--mode", "rpc", "--no-ui", "--no-tools", "--no-extensions", "--no-skills",
    "--no-rules", "--no-context-files", "--no-lsp", "--no-pty", "--no-title", "--no-session",
)
_SUMMARY_LIMIT = 16 * 1024


class OmpBackendError(RuntimeError):
    pass


async def _spawn(spec: SandboxLaunchSpec) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *spec.argv, cwd=spec.cwd, env=dict(spec.parent_env),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=1024 * 1024 + 1,
    )


@dataclass(frozen=True, slots=True)
class OmpBackendDependencies:
    executable: ResolvedExecutable
    sandbox: Callable[[SandboxRequest], SandboxLaunchSpec] = field(repr=False)
    parent_environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    spawn: Callable[[SandboxLaunchSpec], Awaitable[Any]] = field(default=_spawn, repr=False)
    connect: Callable[..., Awaitable[Any]] = field(default=connect_gateway, repr=False)
    revalidate: Callable[[ResolvedExecutable], None] = field(default=revalidate_omp, repr=False)
    prepare_runtime: Callable[..., tuple[dict[str, str], str]] = field(
        default=stage_runtime, repr=False,
    )


class OmpAgentBackend:
    def __init__(self, dependencies: OmpBackendDependencies) -> None:
        self._dependencies = dependencies
        descriptor = dependencies.executable
        self._identity = BackendIdentity("omp", descriptor.version, descriptor.sha256)

    @property
    def identity(self) -> BackendIdentity:
        return self._identity

    def _compile(self, project: Path, view: Path, scratch: Path) -> tuple[SandboxLaunchSpec, str]:
        dependencies = self._dependencies
        environment, endpoint = dependencies.prepare_runtime(
            scratch, dependencies.parent_environment,
        )
        command = (
            str(dependencies.executable.path), *_RESTRICTED, "--cwd", str(view),
            "--session-dir", str(scratch / "omp/sessions"),
        )
        request = SandboxRequest(
            project, view, scratch, command, environment,
            runtime_read_roots=(dependencies.executable.path,),
        )
        spec = dependencies.sandbox(request)
        validate_launch_spec(request, spec)
        return spec, endpoint

    async def run(self, request: AgentRequest) -> AgentResult:
        if request.model is not None:
            raise OmpBackendError("OMP_MODEL_OVERRIDE_FORBIDDEN")
        if request.timeout_seconds <= 0:
            raise OmpBackendError("OMP_TIMEOUT_INVALID")
        socket = request.gateway_broker_socket
        if (not socket.is_absolute() or socket.name != "gateway.sock"
                or socket.parent.name != "run" or socket.parent.parent.name != ".aizim"):
            raise OmpBackendError("OMP_PROJECT_ROOT_INVALID")
        dependencies = self._dependencies
        await asyncio.to_thread(dependencies.revalidate, dependencies.executable)
        spec, _endpoint = self._compile(socket.parents[2].resolve(strict=True),
                                        request.view_root, request.scratch_root)
        # The capability session is consumed only here, in the trusted parent process.
        if any(request.gateway_session_id and request.gateway_session_id in value
               for value in (*spec.argv, *spec.parent_env.values(), *spec.shell_env.values())):
            raise OmpBackendError("OMP_SECRET_IN_LAUNCH")
        channel = None
        client = None
        interrupted = False
        try:
            async with asyncio.timeout(request.timeout_seconds):
                channel = await dependencies.connect(socket, request.gateway_session_id)
                tools = GatewayHostTools(channel)
                client = RpcClient(await dependencies.spawn(spec), host_tool=tools)
                await client.start()
                model = _model(await client.request("get_state"))
                inventory = await client.request("set_host_tools", tools=tools.definitions)
                expected = [tool["name"] for tool in tools.definitions]
                if type(inventory) is not dict or inventory.get("toolNames") != expected:
                    raise OmpBackendError("OMP_TOOL_REGISTRATION_FAILED")
                await client.prompt(request.prompt)
                model = _model(await client.request("get_state"))
                summary = _summary(client.messages)
                # An accepted contribution is promoted on its own evidence, so earlier
                # tool errors or refusals do not erase it.
                status = (
                    "submitted" if tools.submitted else "failed" if tools.failed else "abstained"
                )
        except (asyncio.CancelledError, TimeoutError):
            interrupted = True
            raise
        finally:
            async def cleanup() -> None:
                try:
                    if client is not None:
                        if interrupted:
                            with suppress(Exception):
                                await asyncio.wait_for(client.abort(), timeout=0.5)
                        await client.close()
                finally:
                    if channel is not None:
                        await channel.aclose()
            interruption = await await_cleanup(asyncio.create_task(cleanup()))
            if interruption is not None:
                raise interruption
        exit_code = client.exit_code
        if exit_code is None:
            raise OmpBackendError("OMP_EXIT_CODE_UNAVAILABLE")
        if exit_code != 0:
            status = "failed"
        return AgentResult(
            worker_id=request.worker_id, status=status, summary=summary,
            transport_event_hash=client.transport_digest,
            final_message_hash=hashlib.sha256(summary.encode()).hexdigest(),
            exit_code=exit_code, policy_hash=spec.policy_hash,
            usage=client.usage, model=model,
        )


def _model(state: object) -> str:
    if type(state) is dict:
        model = state.get("model")
        if type(model) is dict:
            provider, identifier = model.get("provider"), model.get("id")
            if provider == "magpie" and type(identifier) is str and identifier:
                return f"{provider}/{identifier}"
    raise OmpBackendError("OMP_MODEL_UNAVAILABLE")


def _summary(messages: list[dict]) -> str:
    text = ""
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if type(content) is list:
            text = "\n".join(item["text"] for item in content
                             if type(item) is dict and item.get("type") == "text"
                             and type(item.get("text")) is str)
    return text.encode("utf-8")[:_SUMMARY_LIMIT].decode("utf-8", errors="ignore")


def create_omp_backend(parent_environment: Mapping[str, str] | None = None) -> OmpAgentBackend:
    environment = dict(os.environ if parent_environment is None else parent_environment)
    executable = resolve_omp(environment)
    return OmpAgentBackend(OmpBackendDependencies(
        executable, sandbox_adapter(executable.path).compile, environment,
    ))


async def inspect_omp_worker(project_root: Path,
                             parent_environment: Mapping[str, str] | None = None) -> dict:
    backend = create_omp_backend(parent_environment)
    with tempfile.TemporaryDirectory(prefix="aizim-omp-preflight-") as temporary:
        private = Path(temporary)
        view, scratch = private / "aizim-view-preflight", private / "aizim-scratch-preflight"
        view.mkdir(mode=0o700)
        scratch.mkdir(mode=0o700)
        await asyncio.to_thread(backend._dependencies.revalidate, backend._dependencies.executable)
        spec, endpoint = backend._compile(project_root.resolve(strict=True), view, scratch)
        await asyncio.to_thread(check_magpie, endpoint)
        client = RpcClient(await backend._dependencies.spawn(spec))
        try:
            async with asyncio.timeout(30):
                await client.start()
                state = await client.request("get_state")
                model = _model(state)
                inventory = state.get("dumpTools") if type(state) is dict else None
                if type(inventory) is not list or inventory:
                    raise OmpBackendError("OMP_AMBIENT_TOOLS_PRESENT")
                registered = await client.request("set_host_tools", tools=[])
                if registered != {"toolNames": []}:
                    raise OmpBackendError("OMP_TOOL_REGISTRATION_FAILED")
                return {"model": model, "protocol_version": 2, "tool_names": (),
                        "identity": backend.identity, "policy_hash": spec.policy_hash,
                        "ambient_disabled": True}
        finally:
            interruption = await await_cleanup(asyncio.create_task(client.close()))
            if interruption is not None:
                raise interruption


async def preflight_omp_worker(project_root: Path,
                               parent_environment: Mapping[str, str] | None = None) -> str:
    return (await inspect_omp_worker(project_root, parent_environment))["model"]
