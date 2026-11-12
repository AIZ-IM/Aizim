from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from aizim.agents import omp_backend
from aizim.agents.backend import AgentRequest
from aizim.agents.omp_backend import OmpAgentBackend, OmpBackendDependencies, OmpBackendError
from aizim.agents.sandbox import SandboxLaunchSpec, SandboxRequest
from aizim.domain import AgentRole
from aizim.domain.serialization import JsonValue
from aizim.gateway.capabilities import GatewayError, GatewayFailure, GatewaySuccess, GatewayTool
from aizim.omp.executable import OmpExecutableError
from aizim.omp.runtime import stage_runtime
from aizim.omp.tools import GatewayHostTools
from aizim.runtime.executable import ResolvedExecutable


def contribution_payload() -> dict[str, JsonValue]:
    return {
        "document_id": "document-test",
        "contribution_id": "contribution-test",
        "candidate_name": "Research.add_zero",
        "complete_type": "(n : Nat) : n + 0 = n",
        "imports": ["Std"],
        "dependencies": [],
        "assumptions": [],
        "evidence_links": [],
    }


class FakeGateway:
    tools = (GatewayTool.CONTRIBUTION_SUBMIT,)

    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.calls: list[tuple[GatewayTool | str, dict[str, JsonValue]]] = []
        self.result: GatewaySuccess | GatewayFailure = GatewaySuccess(
            {"contribution_id": "contribution-test"}
        )
        self.results: list[GatewaySuccess | GatewayFailure] = []
        self.error: Exception | None = None
        self.closed = False

    async def call(
        self, operation: GatewayTool | str, payload: dict[str, JsonValue]
    ) -> GatewaySuccess | GatewayFailure:
        self.calls.append((operation, payload))
        if self.error is not None:
            raise self.error
        return self.results.pop(0) if self.results else self.result

    async def aclose(self) -> None:
        self.events.append("gateway-close")
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("state_append", {"payload": contribution_payload()}),
        ("contribution.submit", {"payload": contribution_payload()}),
        ("contribution_submit", None),
        ("contribution_submit", []),
        ("contribution_submit", {}),
        ("contribution_submit", {"payload": {}}),
        ("contribution_submit", {"payload": contribution_payload(), "session_id": "forged"}),
        ("contribution_submit", {"payload": {**contribution_payload(), "run_id": "forged"}}),
        ("contribution_submit", {"payload": {**contribution_payload(), "document_id": 1}}),
        ("contribution_submit", {"payload": contribution_payload(),
                                 "code": "GATEWAY_RATE_LIMITED"}),
        ("contribution_submit", {"payload": contribution_payload(), "retry_after_seconds": 1}),
        ("contribution_submit", {"payload": {
            **contribution_payload(), "retry_after_seconds": 1,
        }}),
        ("contribution_submit", {"payload": {
            **contribution_payload(), "code": "GATEWAY_RATE_LIMITED",
        }}),
        ("contribution_submit", {"payload": contribution_payload(), "call_budget": 10000}),
        ("contribution_submit", {"payload": {
            **contribution_payload(), "call_budget": 10000,
        }}),
        ("contribution_submit", {"payload": {**contribution_payload(), "imports": "Std"}}),
        ("contribution_submit", {"payload": {**contribution_payload(), "assumptions": [False]}}),
    ],
)
async def test_host_tools_reject_invalid_arguments_before_gateway(name, arguments) -> None:
    gateway = FakeGateway()
    tools = GatewayHostTools(gateway)

    text, failed = await tools(name, arguments)

    assert failed is True
    assert json.loads(text) == {"ok": False, "error": {"code": "INVALID_REQUEST"}}
    assert gateway.calls == []
    assert tools.submitted is False


@pytest.mark.asyncio
async def test_host_tools_submit_only_through_trusted_gateway() -> None:
    gateway = FakeGateway()
    tools = GatewayHostTools(gateway)
    payload = contribution_payload()

    text, failed = await tools("contribution_submit", {"payload": payload})

    assert failed is False
    assert json.loads(text) == {
        "ok": True, "result": {"contribution_id": "contribution-test"},
    }
    assert gateway.calls == [(GatewayTool.CONTRIBUTION_SUBMIT, payload)]
    assert tools.submitted is True
    assert tools.failed is False
    assert [definition["name"] for definition in tools.definitions] == ["contribution_submit"]
    assert tools.definitions[0]["parameters"]["additionalProperties"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_host_tools_do_not_count_denied_or_unavailable_submission(unavailable: bool) -> None:
    gateway = FakeGateway()
    gateway.result = GatewayFailure(GatewayError("CAPABILITY_DENIED", "denied", None))
    if unavailable:
        gateway.error = ConnectionError("broker disconnected")
    tools = GatewayHostTools(gateway)

    text, failed = await tools("contribution_submit", {"payload": contribution_payload()})

    assert failed is True
    assert json.loads(text)["error"]["code"] == (
        "GATEWAY_UNAVAILABLE" if unavailable else "CAPABILITY_DENIED"
    )
    assert tools.submitted is False
    assert tools.failed is True


@pytest.mark.asyncio
async def test_host_tools_report_tool_errors_without_failing_the_worker() -> None:
    gateway = FakeGateway()
    gateway.result = GatewayFailure(
        GatewayError("UNVERIFIED_DOCUMENT_APPLY", "gateway operation failed", "event-1")
    )
    tools = GatewayHostTools(gateway)

    text, failed = await tools("contribution_submit", {"payload": contribution_payload()})

    assert failed is True
    assert json.loads(text)["error"]["code"] == "UNVERIFIED_DOCUMENT_APPLY"
    assert tools.failed is False
    assert tools.submitted is False


def model_settings(source: Path) -> dict[str, str]:
    source.mkdir()
    config = {
        "modelRoles": {"default": "magpie/research:high", "advisor": "other/private"},
        "apiKey": "config-provider-secret",
        "extensions": ["/ambient/extension.ts"],
        "mcpServers": {"ambient": {"command": "private-command"}},
        "memory": {"enabled": True},
    }
    models = {"providers": {
        "magpie": {
            "baseUrl": "http://127.0.0.1:8090/v1", "api": "openai-completions",
            "apiKey": "magpie-provider-secret", "auth": "oauth",
            "headers": {"Authorization": "Bearer provider-header-secret"},
            "models": [{
                "id": "research", "name": "private model display name",
                "contextWindow": 131072, "maxTokens": 8192,
                "reasoning": True, "input": ["text", "image"],
                "apiKey": "model-provider-secret", "headers": {"secret": "hidden"},
                "compat": {"extraBody": {"command": "private-command"}},
            }, {"id": "unused", "apiKey": "unused-model-secret"}],
        },
        "other": {"apiKey": "other-provider-secret"},
    }}
    (source / "config.yml").write_text(json.dumps(config))
    (source / "models.yml").write_text(json.dumps(models))
    (source / "auth.json").write_text('{"token":"auth-file-secret"}')
    return {
        "HOME": str(source.parent), "PI_CODING_AGENT_DIR": str(source),
        "OPENAI_API_KEY": "environment-provider-secret",
        "ANTHROPIC_API_KEY": "anthropic-environment-secret",
        "AIZIM_CAPABILITY_TOKEN": "capability-environment-secret",
        "NODE_OPTIONS": "--require /ambient/injection.js",
        "HTTP_PROXY": "http://proxy-with-secret.invalid",
    }


def test_runtime_stages_minimal_model_settings_without_credentials(tmp_path: Path) -> None:
    environment = model_settings(tmp_path / "settings")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    child_environment, endpoint = stage_runtime(scratch, environment)

    assert endpoint == "http://127.0.0.1:8090/v1"
    agent = scratch / "omp/agent"
    assert json.loads((agent / "config.yml").read_text()) == {
        "modelRoles": {"default": "magpie/research:high"},
        "tools": {"xdev": False}, "advisor": {"enabled": False},
        "memory": {"enabled": False}, "providers": {"cacheWarming": "off"},
    }
    assert json.loads((agent / "models.yml").read_text()) == {"providers": {"magpie": {
        "baseUrl": endpoint, "api": "openai-completions", "auth": "none",
        "models": [{
            "id": "research", "name": "research", "contextWindow": 131072,
            "maxTokens": 8192, "reasoning": True, "input": ["text", "image"],
        }],
    }}}
    assert {path.name for path in agent.iterdir()} == {"config.yml", "models.yml"}
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in agent.iterdir())
    assert child_environment == {
        "PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "HOME": str(scratch / "omp/home"), "TMPDIR": str(scratch),
        "PI_CODING_AGENT_DIR": str(agent), "PI_NATIVES_DIR": str(scratch / "omp/natives"),
    }
    staged = "\n".join(path.read_text() for path in agent.iterdir())
    assert "secret" not in staged
    assert "private-command" not in staged
    assert "ambient" not in staged
    assert not any(key in child_environment for key in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AIZIM_CAPABILITY_TOKEN",
        "NODE_OPTIONS", "HTTP_PROXY",
    ))


@pytest.mark.parametrize("endpoint", [
    "https://remote.example/v1", "http://user:password@localhost:8090/v1",
    "http://127.0.0.1:8090/v1?token=secret", "http://127.0.0.1:8090/v1#secret",
])
def test_runtime_rejects_remote_or_credential_bearing_endpoint(
    tmp_path: Path, endpoint: str,
) -> None:
    environment = model_settings(tmp_path / "settings")
    catalog_path = Path(environment["PI_CODING_AGENT_DIR"]) / "models.yml"
    catalog = json.loads(catalog_path.read_text())
    catalog["providers"]["magpie"]["baseUrl"] = endpoint
    catalog_path.write_text(json.dumps(catalog))

    with pytest.raises(OmpExecutableError, match="OMP_MAGPIE_CONFIG_INVALID"):
        stage_runtime(tmp_path / "scratch", environment)

    assert not (tmp_path / "scratch/omp/agent").exists()


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.reaped = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    async def wait(self) -> int:
        self.reaped = True
        assert self.returncode is not None
        return self.returncode


class BackendHarness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[str] = []
        self.gateway = FakeGateway(self.events)
        self.process = FakeProcess()
        self.launches: list[SandboxLaunchSpec] = []
        self.redemptions: list[tuple[Path, str]] = []
        self.revalidated: list[ResolvedExecutable] = []
        self.rpc_requests: list[tuple[str, dict]] = []
        self.tool_results: list[tuple[str, bool]] = []
        self.submit = False
        self.submit_calls = 1
        self.block_prompt = False
        self.prompt_started = asyncio.Event()
        self.aborted = False
        self.client_closed = False
        self.exit_code: int | None = 0
        self.prompt_text: str | None = None
        self.failure: Exception | None = None
        self.summary = '{"status":"submitted","verified":true,"contribution_id":"forged"}'
        self.project = tmp_path / "project"
        self.project.mkdir()
        self.view = tmp_path / "view"
        self.scratch = tmp_path / "scratch"
        self.view.mkdir()
        self.scratch.mkdir()
        self.environment = model_settings(tmp_path / "settings")
        self.executable = ResolvedExecutable(tmp_path / "omp", "omp test", "a" * 64)
        harness = self

        class FakeRpcClient:
            transport_digest = "b" * 64

            def __init__(self, process, *, host_tool) -> None:
                assert process is harness.process
                self.usage = {"input_tokens": 17, "output_tokens": 3}
                self.host_tool = host_tool
                self.messages: list[dict] = []

            async def start(self) -> None:
                harness.events.append("rpc-start")

            async def request(self, command: str, **arguments):
                harness.rpc_requests.append((command, arguments))
                if command == "get_state":
                    return {"model": {"provider": "magpie", "id": "research:high"}}
                assert command == "set_host_tools"
                return {"toolNames": [tool["name"] for tool in arguments["tools"]]}

            async def prompt(self, text: str) -> None:
                harness.prompt_text = text
                harness.prompt_started.set()
                if harness.block_prompt:
                    await asyncio.Event().wait()
                if harness.failure is not None:
                    raise harness.failure
                for _ in range(harness.submit_calls if harness.submit else 0):
                    harness.tool_results.append(await self.host_tool(
                        "contribution_submit", {"payload": contribution_payload()},
                    ))
                self.messages = [{"role": "assistant", "content": [
                    {"type": "text", "text": harness.summary},
                ]}]

            async def abort(self) -> None:
                harness.events.append("rpc-abort")
                harness.aborted = True

            async def close(self) -> None:
                harness.events.append("rpc-close")
                harness.client_closed = True
                harness.process.terminate()
                await harness.process.wait()
                self.exit_code = harness.exit_code

        monkeypatch.setattr(omp_backend, "RpcClient", FakeRpcClient)
        self.dependencies = OmpBackendDependencies(
            executable=self.executable,
            sandbox=self.sandbox,
            parent_environment=self.environment,
            spawn=self.spawn,
            connect=self.connect,
            revalidate=self.revalidate,
            prepare_runtime=stage_runtime,
        )
        self.backend = OmpAgentBackend(self.dependencies)
        self.request = AgentRequest(
            run_id="research-run", worker_id="prover-test", role=AgentRole.PROOF_EXPLORER,
            prompt="Attempt the target through host tools.", model=None,
            view_root=self.view, scratch_root=self.scratch,
            gateway_session_id="broker-session-secret",
            gateway_broker_socket=self.project / ".aizim/run/gateway.sock", timeout_seconds=30,
        )

    def revalidate(self, executable: ResolvedExecutable) -> None:
        self.events.append("revalidate")
        self.revalidated.append(executable)

    def sandbox(self, request: SandboxRequest) -> SandboxLaunchSpec:
        self.events.append("sandbox")
        assert request.project_root == self.project
        assert request.runtime_read_roots == (self.executable.path,)
        return SandboxLaunchSpec(
            platform_id="linux", argv=("/fake/sandbox", *request.command),
            cwd=request.view_root, parent_env=dict(request.parent_env), shell_env={},
            view_root=request.view_root, scratch_root=request.scratch_root,
            profile_id="aizim-worker", policy_hash="c" * 64,
        )

    async def spawn(self, spec: SandboxLaunchSpec) -> FakeProcess:
        self.events.append("spawn")
        self.launches.append(spec)
        return self.process

    async def connect(self, socket: Path, session: str) -> FakeGateway:
        self.events.append("connect")
        self.redemptions.append((socket, session))
        return self.gateway


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BackendHarness:
    return BackendHarness(tmp_path, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize(("submission", "denied", "status"), [
    (False, False, "abstained"), (True, False, "submitted"), (True, True, "failed"),
])
async def test_backend_status_comes_from_gateway_not_assistant_text(
    harness: BackendHarness, submission: bool, denied: bool, status: str,
) -> None:
    harness.submit = submission
    if denied:
        harness.gateway.result = GatewayFailure(GatewayError("CAPABILITY_DENIED", "denied", None))

    result = await harness.backend.run(harness.request)

    assert result.status == status
    assert result.summary == harness.summary
    assert result.final_message_hash == hashlib.sha256(harness.summary.encode()).hexdigest()
    assert result.transport_event_hash == "b" * 64
    assert result.policy_hash == "c" * 64
    assert result.usage == {"input_tokens": 17, "output_tokens": 3}
    assert result.model == "magpie/research:high"
    assert result.worker_id == harness.request.worker_id
    assert harness.prompt_text == harness.request.prompt
    assert len(harness.gateway.calls) == int(submission)
    assert harness.client_closed and harness.gateway.closed and harness.process.reaped
    assert harness.aborted is False


TOOL_ERROR = GatewayFailure(
    GatewayError("UNVERIFIED_DOCUMENT_APPLY", "gateway operation failed", "event-1")
)
DENIED = GatewayFailure(GatewayError("CAPABILITY_DENIED", "denied", "event-2"))
ACCEPTED = GatewaySuccess({"contribution_id": "contribution-test"})


@pytest.mark.asyncio
@pytest.mark.parametrize(("results", "status"), [
    ((TOOL_ERROR, ACCEPTED), "submitted"),
    ((DENIED, ACCEPTED), "submitted"),
    ((TOOL_ERROR,), "abstained"),
    ((TOOL_ERROR, DENIED), "failed"),
])
async def test_backend_status_prefers_a_trusted_submission_over_earlier_errors(
    harness: BackendHarness,
    results: tuple[GatewaySuccess | GatewayFailure, ...],
    status: str,
) -> None:
    harness.submit = True
    harness.submit_calls = len(results)
    harness.gateway.results = list(results)

    result = await harness.backend.run(harness.request)

    assert result.status == status
    assert len(harness.gateway.calls) == len(results)
    assert [failed for _text, failed in harness.tool_results] == [
        isinstance(item, GatewayFailure) for item in results
    ]


@pytest.mark.asyncio
async def test_backend_backs_off_throttled_submission_and_continues(
    harness: BackendHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays = []

    async def backoff(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("aizim.omp.tools.asyncio.sleep", backoff)
    harness.submit = True
    harness.gateway.results = [
        GatewayFailure(GatewayError("GATEWAY_RATE_LIMITED", "retry later", "event-3", 0.25)),
        GatewayFailure(GatewayError("GATEWAY_RATE_LIMITED", "retry later", "event-4", 0.5)),
        ACCEPTED,
    ]

    result = await harness.backend.run(harness.request)

    assert result.status == "submitted"
    assert delays == [0.25, 0.5]
    assert len(harness.gateway.calls) == 3
    assert all(call == harness.gateway.calls[0] for call in harness.gateway.calls)
    assert len(harness.tool_results) == 1
    assert harness.tool_results[0][1] is False
    assert harness.client_closed and harness.gateway.closed and harness.process.reaped


@pytest.mark.asyncio
async def test_backend_cancels_pending_rate_limit_backoff(
    harness: BackendHarness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    waiting = asyncio.Event()

    async def backoff(delay: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("aizim.omp.tools.asyncio.sleep", backoff)
    harness.submit = True
    harness.gateway.result = GatewayFailure(
        GatewayError("GATEWAY_RATE_LIMITED", "retry later", "event-3", 60.0)
    )
    task = asyncio.create_task(harness.backend.run(harness.request))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert len(harness.gateway.calls) == 1
    assert harness.aborted and harness.client_closed
    assert harness.gateway.closed and harness.process.reaped


@pytest.mark.asyncio
async def test_backend_rejects_missing_exit_code_after_cleanup(harness: BackendHarness) -> None:
    harness.exit_code = None

    with pytest.raises(OmpBackendError, match=r"^OMP_EXIT_CODE_UNAVAILABLE$"):
        await harness.backend.run(harness.request)

    assert harness.client_closed and harness.gateway.closed and harness.process.reaped
    assert harness.events[-2:] == ["rpc-close", "gateway-close"]


@pytest.mark.asyncio
async def test_backend_revalidates_image_and_keeps_secrets_out_of_child_launch(
    harness: BackendHarness,
) -> None:
    await harness.backend.run(harness.request)

    assert harness.revalidated == [harness.executable]
    assert harness.events[:5] == ["revalidate", "sandbox", "connect", "spawn", "rpc-start"]
    assert harness.backend.identity.executable_sha256 == harness.executable.sha256
    assert harness.redemptions == [(
        harness.request.gateway_broker_socket, harness.request.gateway_session_id,
    )]
    spec, = harness.launches
    launch = json.dumps([spec.argv, dict(spec.parent_env), dict(spec.shell_env)])
    for secret in (
        harness.request.gateway_session_id, "environment-provider-secret",
        "capability-environment-secret", "anthropic-environment-secret", "auth-file-secret",
    ):
        assert secret not in launch
    for flag in (
        "--no-ui", "--no-tools", "--no-extensions", "--no-skills", "--no-rules",
        "--no-context-files", "--no-session", "--no-pty", "--no-lsp",
    ):
        assert flag in spec.argv
    assert spec.argv[spec.argv.index("--mode") + 1] == "rpc"
    assert spec.argv[spec.argv.index("--cwd") + 1] == str(harness.view)
    assert [command for command, _arguments in harness.rpc_requests] == [
        "get_state", "set_host_tools", "get_state",
    ]


@pytest.mark.asyncio
async def test_backend_cancellation_aborts_closes_and_reaps_before_releasing_gateway(
    harness: BackendHarness,
) -> None:
    harness.block_prompt = True
    task = asyncio.create_task(harness.backend.run(harness.request))
    try:
        await asyncio.wait_for(harness.prompt_started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert harness.aborted
    assert harness.client_closed and harness.gateway.closed
    assert harness.process.terminated and harness.process.reaped
    assert harness.events[-3:] == ["rpc-abort", "rpc-close", "gateway-close"]


@pytest.mark.asyncio
async def test_backend_failure_still_closes_child_and_gateway(harness: BackendHarness) -> None:
    harness.failure = RuntimeError("RPC prompt failed")

    with pytest.raises(RuntimeError, match="RPC prompt failed"):
        await harness.backend.run(harness.request)

    assert harness.client_closed and harness.gateway.closed and harness.process.reaped
    assert harness.events[-2:] == ["rpc-close", "gateway-close"]


@pytest.mark.asyncio
async def test_backend_image_revalidation_failure_prevents_redemption_and_spawn(
    harness: BackendHarness,
) -> None:
    def changed(_executable: ResolvedExecutable) -> None:
        raise OmpExecutableError("OMP_IMAGE_CHANGED")

    backend = OmpAgentBackend(replace(harness.dependencies, revalidate=changed))
    with pytest.raises(OmpExecutableError, match="OMP_IMAGE_CHANGED"):
        await backend.run(harness.request)

    assert harness.redemptions == []
    assert harness.launches == []


@pytest.mark.asyncio
async def test_backend_rejects_model_override_before_launch(harness: BackendHarness) -> None:
    with pytest.raises(OmpBackendError, match="OMP_MODEL_OVERRIDE_FORBIDDEN"):
        await harness.backend.run(replace(harness.request, model="other/model"))

    assert harness.events == []
