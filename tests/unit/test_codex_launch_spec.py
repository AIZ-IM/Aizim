from __future__ import annotations

import asyncio
import json
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from aizim.agents.backend import AgentRequest
from aizim.agents.codex_backend import (
    CodexBackend,
    CodexBackendDependencies,
    CodexBackendError,
    build_codex_launch_spec,
)
from aizim.agents.launcher import AgentLaunchError, CodexLaunchOutcome, launch_codex
from aizim.agents.macos_profile import compile_macos_profile
from aizim.agents.sandbox import SandboxLaunchSpec, SandboxRequest
from aizim.domain import AgentRole, sha256_file
from aizim.runtime.provider_executables import ResolvedExecutable


def request(tmp_path: Path, *, model: str | None = "gpt-5.2-codex") -> AgentRequest:
    view = tmp_path / "aizim-view-fixture"
    scratch = tmp_path / "aizim-scratch-fixture"
    project = tmp_path / "canonical-project"
    view.mkdir()
    scratch.mkdir()
    project.mkdir()
    return AgentRequest(
        run_id="run-7",
        worker_id="worker-7",
        role=AgentRole.FORMALIZER,
        prompt="formalize without leaking this prompt",
        model=model,
        view_root=view,
        scratch_root=scratch,
        gateway_session_id="session-7",
        gateway_broker_socket=project / ".aizim" / "run" / "gateway.sock",
        timeout_seconds=30.0,
    )


def sandbox_spec(agent_request: AgentRequest) -> SandboxLaunchSpec:
    return compile_macos_profile(
        Path("/opt/aizim/bin/codex"),
        SandboxRequest(
            agent_request.gateway_broker_socket.parents[2],
            agent_request.view_root,
            agent_request.scratch_root,
            ("/usr/bin/true",),
            {"PATH": "/usr/bin", "OPENAI_API_KEY": "launcher-owned-credential"},
        ),
        developer_root(agent_request),
    )


def developer_root(agent_request: AgentRequest) -> Path:
    return agent_request.view_root.parent / "approved-developer-root"


def _mcp_table(argv: tuple[str, ...]) -> dict[str, object]:
    override = next(value for value in argv if value.startswith("mcp_servers.aizim="))
    return tomllib.loads(f"value={override.partition('=')[2]}")["value"]


@pytest.mark.parametrize("model", ["gpt-5.2-codex", "gpt-6-astra"])
def test_codex_launch_reuses_profile_and_adds_required_sidecar(tmp_path: Path, model: str) -> None:
    agent_request = request(tmp_path, model=model)
    sandbox = sandbox_spec(agent_request)
    sidecar = Path("/opt/aizim/bin/aizim-gateway-sidecar")

    launch = build_codex_launch_spec(agent_request, sandbox, sidecar)

    assert launch.argv[:9] == sandbox.argv[:9]
    assert launch.argv.index("--strict-config") < launch.argv.index("exec")
    for flag in (
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--json",
        "--output-schema",
        "--output-last-message",
        "-C",
    ):
        assert flag in launch.argv
    assert launch.argv[-1] == "-"
    assert launch.stdin == agent_request.prompt.encode()
    assert agent_request.prompt not in launch.argv
    assert launch.cwd == agent_request.view_root
    assert launch.timeout_seconds == 30.0
    assert launch.parent_env["OPENAI_API_KEY"] == "launcher-owned-credential"
    assert "launcher-owned-credential" not in repr(launch)
    assert "launcher-owned-credential" not in repr(launch.argv)
    assert "--model" in launch.argv
    assert launch.argv[launch.argv.index("--model") + 1] == model

    table = _mcp_table(launch.argv)
    assert table == {
        "command": str(sidecar),
        "args": [
            "--broker-socket",
            str(agent_request.gateway_broker_socket),
            "--session-id",
            "session-7",
        ],
        "startup_timeout_sec": 10,
        "tool_timeout_sec": 60,
        "required": True,
        "default_tools_approval_mode": "approve",
    }


def test_codex_launch_omits_model_and_all_bypass_routes(tmp_path: Path) -> None:
    agent_request = request(tmp_path, model=None)

    launch = build_codex_launch_spec(
        agent_request,
        sandbox_spec(agent_request),
        Path("/opt/aizim/bin/aizim-gateway-sidecar"),
    )

    forbidden = (
        "--dangerously-" + "bypass-approvals-and-sandbox",
        "--dangerously-" + "bypass-hook-trust",
        "--add-" + "dir",
        "--ask-for-" + "approval",
    )
    assert "--model" not in launch.argv
    assert all(value not in launch.argv for value in forbidden)
    assert "/canonical/project" not in launch.argv
    assert "raw-capability-value" not in repr(launch)


def test_alignment_request_uses_a_closed_verdict_schema(tmp_path: Path) -> None:
    agent_request = replace(request(tmp_path), result_schema="alignment")

    launch = build_codex_launch_spec(
        agent_request,
        sandbox_spec(agent_request),
        Path("/opt/aizim/bin/aizim-gateway-sidecar"),
    )
    schema = json.loads(launch.output_schema_path.read_text())

    assert launch.argv[launch.argv.index("--output-schema") + 1] == str(launch.output_schema_path)
    assert launch.output_schema_path.name == "codex_alignment_result.schema.json"
    assert schema["required"] == ["status", "summary"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["summary"] == {"enum": ["aligned", "misaligned"]}


def _fake_codex(path: Path) -> Path:
    path.write_text(
        "#!/usr/bin/python3\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "message_path = argv[argv.index('--output-last-message') + 1]\n"
        "capture = {'argv': argv, 'credential': os.environ.get('OPENAI_API_KEY'), "
        "'stdin': sys.stdin.read()}\n"
        "with open(os.environ['AIZIM_CAPTURE'], 'w') as stream:\n"
        "    json.dump(capture, stream, sort_keys=True)\n"
        "print(json.dumps({'type': 'turn.started', 'turn_id': 'turn-7'}, sort_keys=True))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1}}, "
        "sort_keys=True))\n"
        "with open(message_path, 'w') as stream:\n"
        "    json.dump({'status': 'submitted', 'summary': 'fixture complete'}, stream, "
        "sort_keys=True)\n"
    )
    path.chmod(0o700)
    return path


async def test_launcher_executes_fake_codex_with_parent_environment_split(
    tmp_path: Path,
) -> None:
    agent_request = request(tmp_path)
    executable = _fake_codex(tmp_path / "codex")
    capture = tmp_path / "capture.json"
    base = sandbox_spec(agent_request)
    sandbox = replace(
        base,
        argv=(str(executable), *base.argv[1:]),
        parent_env={
            "PATH": "/usr/bin:/bin",
            "OPENAI_API_KEY": "launcher-owned-credential",
            "AIZIM_CAPTURE": str(capture),
        },
    )
    launch = build_codex_launch_spec(
        agent_request,
        sandbox,
        Path("/opt/aizim/bin/aizim-gateway-sidecar"),
    )

    outcome = await launch_codex(launch)
    recorded = json.loads(capture.read_text())

    assert recorded == {
        "argv": list(launch.argv[1:]),
        "credential": "launcher-owned-credential",
        "stdin": agent_request.prompt,
    }
    assert outcome.status == "submitted"
    assert outcome.summary == "fixture complete"
    assert outcome.exit_code == 0
    assert len(outcome.transport_event_hash) == 64
    assert len(outcome.final_message_hash) == 64
    environment_override = next(
        value for value in launch.argv if value.startswith("shell_environment_policy=")
    )
    assert "OPENAI_API_KEY" not in environment_override
    assert "launcher-owned-credential" not in environment_override


@pytest.mark.parametrize("mode", ("success", "failure", "cancelled", "replaced"))
async def test_codex_backend_has_verified_identity_and_always_finalizes(
    tmp_path: Path, mode: str
) -> None:
    agent_request = request(tmp_path)
    executable = _fake_codex(tmp_path / "codex")
    actions: list[str] = []

    async def launch(_spec):
        actions.append("launch")
        if mode == "failure":
            raise AgentLaunchError("CODEX_PROCESS_FAILED")
        if mode == "cancelled":
            raise asyncio.CancelledError
        return CodexLaunchOutcome("submitted", "fixture complete", "1" * 64, "2" * 64, 0)

    async def revoke(_request: AgentRequest) -> None:
        actions.append("revoke")

    async def cleanup(_request: AgentRequest) -> None:
        actions.append("cleanup")

    base = sandbox_spec(agent_request)
    dependencies = CodexBackendDependencies(
        codex_executable=ResolvedExecutable(
            executable.resolve(),
            "codex-cli 0.154.0",
            sha256_file(executable),
        ),
        codex_version=lambda _path: "codex-cli 0.154.0",
        sandbox=lambda _request: replace(base, argv=(str(executable), *base.argv[1:])),
        sidecar_executable=Path("/opt/aizim/bin/aizim-gateway-sidecar"),
        launch=launch,
        revoke=revoke,
        cleanup=cleanup,
    )
    original_hash = sha256_file(executable)
    backend = CodexBackend(dependencies)
    if mode == "replaced":
        executable.write_text("replacement")

    if mode == "success":
        result = await backend.run(agent_request)
        assert result.status == "submitted"
        assert result.policy_hash == base.policy_hash
    else:
        expected = {
            "failure": AgentLaunchError,
            "cancelled": asyncio.CancelledError,
            "replaced": CodexBackendError,
        }[mode]
        with pytest.raises(expected):
            await backend.run(agent_request)

    assert backend.identity.executable_sha256 == original_hash
    assert backend.identity.version == "codex-cli 0.154.0"
    expected_actions = (
        ["revoke", "cleanup"]
        if mode == "replaced"
        else [
            "launch",
            "revoke",
            "cleanup",
        ]
    )
    assert actions == expected_actions
