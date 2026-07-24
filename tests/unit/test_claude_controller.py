from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path

import pytest

import aizim.orchestration.claude_controller as claude_module
from aizim.agents.sandbox import SandboxLaunchSpec, SandboxRequest
from aizim.domain import AgentRole, canonical_json, sha256_file
from aizim.orchestration.claude_controller import (
    ClaudeControllerBackend,
    ClaudeControllerError,
    production_controller,
)
from aizim.orchestration.codex_controller import CodexControllerBackend
from aizim.orchestration.control_plane import ControllerProvider
from aizim.orchestration.controller_backend import (
    ControllerBackendError,
    ControllerContext,
    DispatchDecision,
)
from aizim.orchestration.controller_process import (
    ControllerLauncher,
    ControllerLaunchOutcome,
    ControllerLaunchSpec,
)

type Captured = list[ControllerLaunchSpec]
_CODEX_SCRIPT = "import sys\nprint('codex-cli 0.145.0'if'--version'in sys.argv else'')\n"
_CLAUDE_SCRIPT = "import sys\nprint('2.1.218 (Claude Code)'if'--version'in sys.argv else'')\n"
_DECISION = json.loads(
    b'{"action":"dispatch","worker_id":"proof-a","instruction":"rfl",'
    b'"budget":2,"timeout_seconds":10}'
)


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


def executable(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path.resolve()


def binaries(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    codex = executable(tmp_path, "codex", _CODEX_SCRIPT)
    claude = executable(tmp_path, "claude", _CLAUDE_SCRIPT)
    return codex, claude, {"PATH": str(tmp_path)}


def context(timeout: float = 20.0) -> ControllerContext:
    return ControllerContext(
        "assignment",
        3,
        "prove True",
        "proof-a",
        AgentRole.FORMALIZER,
        "project",
        "a" * 64,
        4,
        ("project.read",),
        5,
        timeout,
        2,
    )


def launcher(
    captured: Captured,
    envelope: bytes | None = None,
    exit_code: int = 0,
) -> ControllerLauncher:
    output = (
        json.dumps(
            {
                "result": {"action": "reject", "reason_code": "TASK_UNSAFE"},
                "session_id": "must-not-persist",
                "structured_output": _DECISION,
            },
            separators=(",", ":"),
        ).encode()
        if envelope is None
        else envelope
    )

    async def launch(spec: ControllerLaunchSpec) -> ControllerLaunchOutcome:
        captured.append(spec)
        return ControllerLaunchOutcome(
            output, hashlib.sha256(b"secret-stderr").hexdigest(), exit_code
        )

    return launch


def test_production_factory_routes_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _codex, claude, environment = binaries(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    backend = ClaudeControllerBackend(claude, None, project, environment, launcher([]))
    monkeypatch.setattr(claude_module, "production_claude", lambda *_arguments: backend)
    assert production_controller(project, ControllerProvider.CLAUDE, None) is backend


@pytest.mark.parametrize("model", [None, "claude-opus-4-6"])
async def test_plan_uses_exact_claude_argv_and_structured_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str | None,
) -> None:
    _codex, claude, environment = binaries(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    captured: Captured = []
    monkeypatch.setattr(
        "aizim.orchestration.claude_controller.sandbox_adapter",
        lambda _executable: PassthroughSandbox(),
    )
    backend = ClaudeControllerBackend(claude, model, project, environment, launcher(captured))

    assert await backend.plan(context()) == DispatchDecision("dispatch", "proof-a", "rfl", 2, 10.0)
    spec = captured[-1]
    schema_path = (
        Path(__file__).parents[2] / "src/aizim/orchestration/controller_decision.schema.json"
    )
    schema = canonical_json(json.loads(schema_path.read_text())).decode()
    model_args = () if model is None else ("--model", model)
    assert spec.argv == (
        str(claude),
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        schema,
        "--safe-mode",
        "--disable-slash-commands",
        "--setting-sources",
        "",
        "--permission-mode",
        "plan",
        "--no-chrome",
        "--tools",
        "",
        "--disallowedTools",
        "mcp__*",
        "--strict-mcp-config",
        "--no-session-persistence",
        *model_args,
    )
    assert "bypassPermissions" not in spec.argv
    assert spec.cwd.name.startswith("aizim-view-")
    assert not spec.cwd.exists()


async def test_provider_stdin_is_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex, claude, environment = binaries(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    codex_captured: Captured = []
    claude_captured: Captured = []
    monkeypatch.setattr(
        "aizim.orchestration.codex_controller.sandbox_adapter",
        lambda _executable: PassthroughSandbox(),
    )
    monkeypatch.setattr(
        "aizim.orchestration.claude_controller.sandbox_adapter",
        lambda _executable: PassthroughSandbox(),
    )

    async def launch_codex(spec: ControllerLaunchSpec) -> ControllerLaunchOutcome:
        codex_captured.append(spec)
        final = Path(spec.argv[spec.argv.index("--output-last-message") + 1])
        final.write_bytes(canonical_json(_DECISION))
        return ControllerLaunchOutcome(b"", "0" * 64, 0)

    await CodexControllerBackend(codex, None, project, environment, launch_codex).plan(context())
    await ClaudeControllerBackend(
        claude, None, project, environment, launcher(claude_captured)
    ).plan(context())

    assert codex_captured[-1].stdin == claude_captured[-1].stdin


async def test_preflight_checks_image_version_and_auth_without_exposing_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _codex, claude, environment = binaries(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    captured: Captured = []
    secret = "anthropic-direct-auth-secret"
    environment |= {"HOME": str(tmp_path / "auth-home"), "ANTHROPIC_API_KEY": secret}
    monkeypatch.setattr(
        "aizim.orchestration.claude_controller.sandbox_adapter",
        lambda _executable: PassthroughSandbox(),
    )
    backend = ClaudeControllerBackend(
        claude, None, project, environment, launcher(captured, b'{"auth":"secret-payload"}')
    )

    await backend.preflight()

    assert backend.identity.version == "2.1.218 (Claude Code)"
    assert backend.identity.executable_sha256 == sha256_file(claude)
    assert captured[0].argv == (str(claude), "auth", "status", "--json")
    assert captured[0].stdin == b""
    assert captured[0].env["ANTHROPIC_API_KEY"] == secret
    assert secret not in repr(captured[0])
    assert all(secret not in value for value in captured[0].argv)


@pytest.mark.parametrize(
    ("envelope", "exit_code", "code"),
    [
        (b"not-json", 0, "CONTROLLER_RESULT_INVALID"),
        (b"{}", 0, "CONTROLLER_RESULT_INVALID"),
        (b'{"structured_output":[]}', 0, "CONTROLLER_RESULT_INVALID"),
        (b"x" * (1024 * 1024 + 1), 0, "CONTROLLER_RESULT_OVERSIZED"),
        (
            b'{"structured_output":{"action":"blocked","reason_code":"NO_SAFE_ACTION"}}',
            9,
            "CONTROLLER_PROCESS_FAILED",
        ),
    ],
    ids=("malformed", "missing", "wrong-type", "oversized", "nonzero"),
)
async def test_plan_rejects_invalid_or_failed_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    envelope: bytes,
    exit_code: int,
    code: str,
) -> None:
    _codex, claude, environment = binaries(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        "aizim.orchestration.claude_controller.sandbox_adapter",
        lambda _executable: PassthroughSandbox(),
    )
    backend = ClaudeControllerBackend(
        claude, None, project, environment, launcher([], envelope, exit_code)
    )

    with pytest.raises(ControllerBackendError, match="CONTROLLER_DECISION_INVALID") as caught:
        await backend.plan(context())
    cause = caught.value.__cause__
    assert type(cause) is ClaudeControllerError
    assert str(cause) == code


async def test_plan_rejects_executable_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _codex, claude, environment = binaries(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        "aizim.orchestration.claude_controller.sandbox_adapter",
        lambda _executable: PassthroughSandbox(),
    )
    backend = ClaudeControllerBackend(claude, None, project, environment, launcher([]))
    claude.write_bytes(claude.read_bytes() + b"\n")

    with pytest.raises(ControllerBackendError, match="CONTROLLER_DECISION_INVALID") as caught:
        await backend.plan(context())
    assert str(caught.value.__cause__) == "CONTROLLER_IMAGE_CHANGED"
