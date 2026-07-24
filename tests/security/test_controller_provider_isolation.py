from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from aizim.domain import AgentRole
from aizim.orchestration.codex_controller import CodexControllerBackend
from aizim.orchestration.codex_worker import preflight_codex_worker
from aizim.orchestration.controller_backend import ControllerContext
from aizim.orchestration.controller_process import (
    ControllerLaunchError,
    ControllerLaunchOutcome,
    ControllerLaunchSpec,
    run_controller_host_command,
)


def executable(tmp_path: Path) -> Path:
    root = tmp_path / "provider-bin"
    root.mkdir()
    path = root / "codex"
    path.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "sys.stdin.buffer.read()\n"
        "print('codex-cli 0.145.0' if '--version' in sys.argv else '')\n"
    )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path.resolve()


def test_sync_host_command_counts_output_after_other_stream_closes(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "output"
    binary.write_text(f"#!{sys.executable}\nimport os\nos.close(2)\nprint('x'*2048)\n")
    binary.chmod(0o700)

    with pytest.raises(ControllerLaunchError, match="CONTROLLER_OUTPUT_LIMIT"):
        run_controller_host_command((str(binary),), {}, 1024)


async def test_backend_constructor_discovers_version_inside_running_loop(
    tmp_path: Path,
) -> None:
    binary = executable(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    backend = CodexControllerBackend(binary, None, project, {})

    assert backend.identity.version == "codex-cli 0.145.0"


async def test_controller_provider_cannot_observe_project_or_authority_secrets(
    tmp_path: Path,
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
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "real-home"),
        "OPENAI_API_KEY": "openai-direct-auth",
        "NPM_TOKEN": npm_secret,
        "DATABASE_URL": unrelated,
        "AIZIM_CAPABILITY": capability,
        "AIZIM_STATE_SOCKET": str(planted["state.sock"]),
    }
    observed: list[bytes] = []

    async def launch(spec: ControllerLaunchSpec) -> ControllerLaunchOutcome:
        view_entries = tuple(spec.cwd.iterdir())
        policy = 'shell_environment_policy.inherit="none"'
        provider_argv = spec.argv[spec.argv.index(policy) - 1 :]
        observed.extend(
            (
                "\0".join(provider_argv).encode(),
                "\0".join(f"{key}={value}" for key, value in spec.env.items()).encode(),
                spec.stdin,
                repr(view_entries).encode(),
            )
        )
        final = Path(spec.argv[spec.argv.index("--output-last-message") + 1])
        final.write_bytes(b'{"action":"blocked","reason_code":"NO_SAFE_ACTION"}')
        return ControllerLaunchOutcome(b"", hashlib.sha256(b"").hexdigest(), 0)

    backend = CodexControllerBackend(
        executable(tmp_path),
        None,
        project,
        environment,
        launch,
    )
    context = ControllerContext(
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
        5.0,
        1,
    )

    await backend.plan(context)

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
    assert b"OPENAI_API_KEY=openai-direct-auth" in combined
    assert not tuple(tmp_path.glob("aizim-controller-*"))


async def test_worker_preflight_compiles_private_probe_without_model(
    tmp_path: Path,
) -> None:
    project = tmp_path / "worker-project"
    project.mkdir()
    binary = executable(tmp_path)

    await preflight_codex_worker(project, {"PATH": str(binary.parent)})

    assert not tuple(tmp_path.glob("aizim-worker-*"))
