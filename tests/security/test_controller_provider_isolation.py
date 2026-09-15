from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest
from integration.cli_control_support import initialized_project, run_cli

from aizim.agents.sandbox import SandboxLaunchSpec, SandboxRequest
from aizim.domain import AgentRole, ControllerProviderId
from aizim.orchestration.claude_controller import ClaudeControllerBackend
from aizim.orchestration.codex_controller import CodexControllerBackend
from aizim.orchestration.codex_worker import preflight_codex_worker
from aizim.orchestration.controller_backend import (
    BlockedDecision,
    ControllerBackend,
    ControllerContext,
)
from aizim.orchestration.controller_process import (
    ControllerLaunchOutcome,
    ControllerLaunchSpec,
    launch_controller_process,
)
from aizim.runtime.provider_executables import ResolvedExecutable

CODEX = ControllerProviderId("codex")
CLAUDE = ControllerProviderId("claude")


class PassthroughSandbox:
    def compile(self, request: SandboxRequest) -> SandboxLaunchSpec:
        return SandboxLaunchSpec(
            "darwin" if sys.platform == "darwin" else "linux",
            request.command,
            request.view_root,
            request.parent_env,
            {},
            request.view_root,
            request.scratch_root,
            "aizim-worker",
            "a" * 64,
        )


def executable(tmp_path: Path, provider: ControllerProviderId = CODEX) -> Path:
    root = tmp_path / "provider-bin"
    root.mkdir(exist_ok=True)
    path = root / provider.value
    version = {
        CODEX: "codex-cli 0.154.0",
        CLAUDE: "2.1.218 (Claude Code)",
    }[provider]
    path.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "sys.stdin.buffer.read()\n"
        f"print({version!r} if '--version' in sys.argv else '')\n"
    )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    if provider == CODEX:
        resources = tmp_path / "codex-resources"
        resources.mkdir(exist_ok=True)
        bwrap = resources / "bwrap"
        bwrap.write_text("#!/bin/sh\nexit 0\n")
        bwrap.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path.resolve()


def descriptor(path: Path, provider: ControllerProviderId) -> ResolvedExecutable:
    canonical = path.resolve()
    version = "codex-cli 0.154.0" if provider == CODEX else "2.1.218 (Claude Code)"
    return ResolvedExecutable(
        canonical,
        version,
        hashlib.sha256(canonical.read_bytes()).hexdigest(),
    )


def controller_context(timeout: float = 5.0) -> ControllerContext:
    return ControllerContext(
        "assignment",
        1,
        "prove True",
        "proof-a",
        AgentRole.FORMALIZER,
        "project-id",
        "a" * 64,
        0,
        ("project.read",),
        1,
        timeout,
        1,
    )


def test_unregistered_provider_id_is_never_executed_from_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = initialized_project(tmp_path)
    marker = tmp_path / "provider-executed"
    command = tmp_path / "bin/future_provider-1"
    command.parent.mkdir()
    command.write_text(f"#!/bin/sh\n/usr/bin/touch {marker}\n")
    command.chmod(0o755)
    monkeypatch.setenv("PATH", str(command.parent))

    configured = run_cli(
        "controller",
        "configure",
        "--project",
        str(root),
        "--provider",
        "future_provider-1",
    )

    assert configured.returncode == 4
    assert configured.stderr == "aizim controller: controller provider is unsupported\n"
    assert not marker.exists()


async def test_backend_constructor_discovers_version_inside_running_loop(
    tmp_path: Path,
) -> None:
    binary = executable(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    backend = CodexControllerBackend(descriptor(binary, CODEX), None, project, {})

    assert backend.identity.version == "codex-cli 0.154.0"


@pytest.mark.parametrize("provider", (CODEX, CLAUDE))
async def test_controller_provider_cannot_observe_project_or_authority_secrets(
    tmp_path: Path, provider: ControllerProviderId
) -> None:
    project = tmp_path / "canonical-project"
    state = project / ".aizim/run"
    state.mkdir(parents=True)
    planted = {
        "project-secret": project / "secret.txt",
        "state-secret": project / ".aizim/secret.txt",
        "state.sock": state / "state.sock",
    }
    for secret, path in planted.items():
        path.write_text(secret)
    capability = "capability-secret-value"
    npm_secret = "npm-secret-value"
    unrelated = "unrelated-secret-value"
    codex = executable(tmp_path)
    environment = {
        "PATH": str(codex.parent),
        "HOME": str(tmp_path / "real-home"),
        "OPENAI_API_KEY": "openai-direct-auth",
        "ANTHROPIC_API_KEY": "anthropic-direct-auth",
        "CLAUDE_CODE_OAUTH_TOKEN": "claude-oauth-direct-auth",
        "NPM_TOKEN": npm_secret,
        "DATABASE_URL": unrelated,
        "AIZIM_CAPABILITY": capability,
        "AIZIM_STATE_SOCKET": str(planted["state.sock"]),
    }
    observed: list[bytes] = []
    binary = executable(tmp_path, provider)

    async def launch(spec: ControllerLaunchSpec) -> ControllerLaunchOutcome:
        view_entries = tuple(spec.cwd.iterdir())
        inner = max(index for index, value in enumerate(spec.argv) if value == str(binary))
        provider_argv = spec.argv[inner:]
        observed.extend(
            (
                "\0".join(provider_argv).encode(),
                "\0".join(f"{key}={value}" for key, value in spec.env.items()).encode(),
                spec.stdin,
                repr(view_entries).encode(),
            )
        )
        match provider.value:
            case "codex":
                final = Path(spec.argv[spec.argv.index("--output-last-message") + 1])
                final.write_bytes(b'{"action":"blocked","reason_code":"NO_SAFE_ACTION"}')
                output = b""
            case "claude":
                output = (
                    b'{"session_id":"raw-session-secret","structured_output":'
                    b'{"action":"blocked","reason_code":"NO_SAFE_ACTION"}}'
                )
            case _:
                raise AssertionError("unexpected test provider")
        return ControllerLaunchOutcome(output, hashlib.sha256(b"").hexdigest(), 0)

    match provider.value:
        case "codex":
            backend: ControllerBackend = CodexControllerBackend(
                descriptor(binary, CODEX),
                None,
                project,
                environment,
                launch,
            )
        case "claude":
            backend = ClaudeControllerBackend(
                descriptor(binary, CLAUDE),
                descriptor(codex, CODEX),
                None,
                project,
                environment,
                launch,
            )
        case _:
            raise AssertionError("unexpected test provider")
    await backend.plan(controller_context())

    combined = b"\n".join(observed)
    forbidden = (
        *planted,
        capability,
        npm_secret,
        unrelated,
        str(project),
        str(planted["state.sock"]),
        "mcp_servers",
        "gateway",
        "capability",
        "NPM_TOKEN",
        "DATABASE_URL",
        "AIZIM_",
    )
    for value in forbidden:
        assert value.encode() not in combined
    direct_auth = (
        b"OPENAI_API_KEY=openai-direct-auth"
        if provider == CODEX
        else b"ANTHROPIC_API_KEY=anthropic-direct-auth"
    )
    assert direct_auth in combined
    assert b"raw-session-secret" not in combined
    assert not tuple(tmp_path.glob("aizim-controller-*"))


async def test_worker_preflight_compiles_private_probe_without_model(
    tmp_path: Path,
) -> None:
    project = tmp_path / "worker-project"
    project.mkdir()
    binary = executable(tmp_path)

    await preflight_codex_worker(
        project,
        descriptor(binary, CODEX),
        {"PATH": str(binary.parent)},
    )

    assert not tuple(tmp_path.glob("aizim-worker-*"))


async def test_claude_timeout_reaps_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _codex = executable(tmp_path)
    pid_file = tmp_path / "pid"
    body = (
        "import os,sys\n"
        "if '--version' in sys.argv: print('2.1.218 (Claude Code)')\n"
        f"else:\n open({str(pid_file)!r},'w').write(str(os.getpid()))\n"
        " os.execv('/usr/bin/tail',('tail','-f','/dev/null'))\n"
    )
    claude = executable(tmp_path, CLAUDE)
    claude.write_text(f"#!{sys.executable}\n{body}")
    project = tmp_path / "timeout-project"
    project.mkdir()
    monkeypatch.setattr(
        "aizim.orchestration.claude_controller.sandbox_adapter",
        lambda _executable: PassthroughSandbox(),
    )
    backend = ClaudeControllerBackend(
        descriptor(claude, CLAUDE),
        descriptor(_codex, CODEX),
        None,
        project,
        {"PATH": str(claude.parent)},
        launch_controller_process,
    )

    with pytest.raises(TimeoutError):
        await backend.plan(controller_context(0.2))
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


@pytest.mark.macos_sandbox
async def test_real_outer_sandbox_runs_secret_safe_fake_claude(tmp_path: Path) -> None:
    codex = Path(value) if (value := shutil.which("codex")) else pytest.fail("codex missing")
    node = Path(value) if (value := shutil.which("node")) else pytest.fail("node missing")
    project, home = tmp_path / "manual-project", tmp_path / "approved-home"
    state = project / ".aizim/run"
    state.mkdir(parents=True)
    for path in (project / "secret.txt", state / "state.sock"):
        path.write_text("project-authority-secret")
    binary = executable(tmp_path, CLAUDE)
    script = (
        "#!/bin/sh\nif [ \"$1\" = --version ]; then echo '2.1.218 (Claude Code)'; exit; fi\n"
        '[ "$ANTHROPIC_API_KEY" = approved-anthropic-auth ] || exit 41; '
        '[ "$CLAUDE_CODE_OAUTH_TOKEN" = approved-claude-oauth ] || exit 42\n'
        f'[ "$HOME" = {str(home)!r} ] || exit 43; '
        '[ -z "$NPM_TOKEN$DATABASE_URL$AIZIM_CAPABILITY$AIZIM_STATE_SOCKET" ] || exit 44\n'
        f"cat {str(project / 'secret.txt')!r} >/dev/null 2>&1 && exit 45; "
        f"cat {str(state / 'state.sock')!r} >/dev/null 2>&1 && exit 46\n"
        'echo \'{"structured_output":{"action":"blocked","reason_code":"NO_SAFE_ACTION"}}\'\n'
    )
    binary.write_text(script)
    binary.chmod(0o700)
    secrets = ("approved-anthropic-auth", "approved-claude-oauth", "unrelated-secret")
    environment = {
        "PATH": f"{codex.parent}:{node.parent}:/usr/bin:/bin",
        "HOME": str(home),
        "ANTHROPIC_API_KEY": secrets[0],
        "CLAUDE_CODE_OAUTH_TOKEN": secrets[1],
        "NPM_TOKEN": secrets[2],
        "DATABASE_URL": secrets[2],
        "AIZIM_CAPABILITY": secrets[2],
        "AIZIM_STATE_SOCKET": str(state / "state.sock"),
    }

    async def launch(spec: ControllerLaunchSpec) -> ControllerLaunchOutcome:
        rendered = repr(spec) + "\0".join(spec.argv)
        assert all(secret not in rendered for secret in secrets)
        return await launch_controller_process(spec)

    backend = ClaudeControllerBackend(
        descriptor(binary, CLAUDE),
        descriptor(codex, CODEX),
        None,
        project,
        environment,
        launch,
    )

    decision = await backend.plan(controller_context())
    assert decision == BlockedDecision("blocked", "NO_SAFE_ACTION")
