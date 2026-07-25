from __future__ import annotations

import asyncio  # noqa: ANYIO_OK - verifies asyncio subprocess cancellation
import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from aizim.agents.sandbox import SandboxLaunchSpec, SandboxRequest
from aizim.domain import AgentRole, sha256_file
from aizim.orchestration.codex_controller import (
    CodexControllerBackend,
    CodexControllerError,
)
from aizim.orchestration.controller_backend import (
    ControllerBackendError,
    ControllerContext,
    DispatchDecision,
)
from aizim.orchestration.controller_process import (
    ControllerLauncher,
    ControllerLaunchError,
    ControllerLaunchOutcome,
    ControllerLaunchSpec,
    launch_controller_process,
)
from aizim.runtime.provider_executables import ResolvedExecutable

type Captured = list[ControllerLaunchSpec]
_VERSION_SCRIPT = "import sys\nprint('codex-cli 0.145.0'if'--version'in sys.argv else'')\n"


class PassthroughSandbox:
    @property
    def platform_id(self) -> str:
        return sys.platform

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


def executable(tmp_path: Path, body: str = _VERSION_SCRIPT) -> Path:
    path = tmp_path / "codex"
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path.resolve()


def descriptor(path: Path) -> ResolvedExecutable:
    return ResolvedExecutable(path, "codex-cli 0.145.0", sha256_file(path))


def context() -> ControllerContext:
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
        20.0,
        2,
    )


def final_path(spec: ControllerLaunchSpec) -> Path:
    return Path(spec.argv[spec.argv.index("--output-last-message") + 1])


def launcher(captured: Captured) -> ControllerLauncher:
    async def launch(spec: ControllerLaunchSpec) -> ControllerLaunchOutcome:
        captured.append(spec)
        if "--output-last-message" in spec.argv:
            final_path(spec).write_bytes(
                b'{"action":"dispatch","worker_id":"proof-a","instruction":"rfl",'
                b'"budget":2,"timeout_seconds":10}'
            )
        return ControllerLaunchOutcome(b"", hashlib.sha256(b"").hexdigest(), 0)

    return launch


@pytest.mark.parametrize("model", [None, "gpt-5.6-sol"])
async def test_plan_uses_exact_codex_argv_and_canonical_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str | None,
) -> None:
    binary = executable(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    captured: Captured = []
    monkeypatch.setattr(
        "aizim.orchestration.codex_controller.sandbox_adapter",
        lambda _executable: PassthroughSandbox(),
    )
    backend = CodexControllerBackend(
        descriptor(binary),
        model,
        project,
        {"PATH": os.environ["PATH"]},
        launcher(captured),
    )

    decision = await backend.plan(context())

    assert decision == DispatchDecision("dispatch", "proof-a", "rfl", 2, 10.0)
    spec = captured[-1]
    schema = Path(__file__).parents[2] / "src/aizim/orchestration/controller_decision.schema.json"
    model_args = () if model is None else ("--model", model)
    assert spec.argv == (
        str(binary),
        "-c",
        'shell_environment_policy.inherit="none"',
        "--strict-config",
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--json",
        "--output-schema",
        str(schema.resolve()),
        "--output-last-message",
        str(final_path(spec)),
        "-C",
        str(spec.cwd),
        *model_args,
        "-",
    )
    assert spec.stdin == (
        b'{"allowed_operations":["project.read"],"assignment_id":"assignment",'
        b'"base_epoch":"'
        + b"a"
        * 64
        + b'","knowledge_epoch":4,"max_budget":5,"max_timeout_seconds":20.0,'
        b'"project_id":"project","role":"formalizer","task":"prove True","task_version":3,'
        b'"worker_id":"proof-a"}'
    )
    assert spec.cwd.name.startswith("aizim-view-")
    assert not spec.cwd.exists()


async def test_preflight_checks_version_login_and_discards_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = executable(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    captured: Captured = []
    monkeypatch.setattr(
        "aizim.orchestration.codex_controller.sandbox_adapter",
        lambda _executable: PassthroughSandbox(),
    )
    backend = CodexControllerBackend(
        descriptor(binary),
        None,
        project,
        {"HOME": str(tmp_path / "auth-home"), "PATH": os.environ["PATH"]},
        launcher(captured),
    )

    await backend.preflight()

    assert len(captured) == 1
    assert captured[0].argv[-3:] == (str(binary), "login", "status")
    assert captured[0].stdin == b""
    assert captured[0].env["HOME"] == str(tmp_path / "auth-home")
    assert not captured[0].cwd.exists()


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (None, "CONTROLLER_RESULT_MISSING"),
        (b"x" * (1024 * 1024 + 1), "CONTROLLER_RESULT_OVERSIZED"),
        (b"not-json", "CONTROLLER_RESULT_INVALID"),
    ],
    ids=("missing", "oversized", "non-json"),
)
async def test_plan_rejects_invalid_final_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes | None,
    code: str,
) -> None:
    binary = executable(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        "aizim.orchestration.codex_controller.sandbox_adapter",
        lambda _executable: PassthroughSandbox(),
    )

    async def launch(spec: ControllerLaunchSpec) -> ControllerLaunchOutcome:
        if body is not None:
            final_path(spec).write_bytes(body)
        return ControllerLaunchOutcome(b"", "0" * 64, 0)

    backend = CodexControllerBackend(descriptor(binary), None, project, {}, launch)
    with pytest.raises(ControllerBackendError, match="CONTROLLER_DECISION_INVALID") as caught:
        await backend.plan(context())
    cause = caught.value.__cause__
    assert type(cause) is CodexControllerError
    assert str(cause) == code


async def test_plan_rejects_executable_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = executable(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        "aizim.orchestration.codex_controller.sandbox_adapter",
        lambda _executable: PassthroughSandbox(),
    )
    backend = CodexControllerBackend(descriptor(binary), None, project, {}, launcher([]))
    binary.write_bytes(binary.read_bytes() + b"\n")

    with pytest.raises(ControllerBackendError, match="CONTROLLER_DECISION_INVALID") as caught:
        await backend.plan(context())
    cause = caught.value.__cause__
    assert type(cause) is CodexControllerError
    assert str(cause) == "CONTROLLER_IMAGE_CHANGED"
    assert sha256_file(binary) != backend.identity.executable_sha256


async def test_process_output_limit_and_timeout_reap(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write('x'*2048)\n")
    output.chmod(0o700)
    spec = ControllerLaunchSpec((str(output),), tmp_path, {}, b"", 1.0, 1024)
    with pytest.raises(ControllerLaunchError, match="CONTROLLER_OUTPUT_LIMIT"):
        await launch_controller_process(spec)

    pid_file = tmp_path / "pid"
    timeout = ControllerLaunchSpec(
        (
            "/bin/sh",
            "-c",
            f"echo $$ > {pid_file}; exec /usr/bin/tail -f /dev/null",
        ),
        tmp_path,
        {},
        b"",
        0.2,
        1024,
    )
    with pytest.raises(ControllerLaunchError, match="CONTROLLER_TIMEOUT"):
        await launch_controller_process(timeout)
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

    pid_file.unlink()
    cancelled = asyncio.create_task(
        launch_controller_process(ControllerLaunchSpec(timeout.argv, tmp_path, {}, b"", 60.0, 1024))
    )
    while not pid_file.exists():
        await asyncio.sleep(0.01)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(cancelled, 1.0)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
