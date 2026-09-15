from __future__ import annotations

import asyncio
import hashlib
import json
import os
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
from aizim.agents.macos_profile import compile_macos_profile, validate_macos_profile
from aizim.agents.sandbox import SandboxLaunchSpec, SandboxRequest
from aizim.domain import AgentRole
from aizim.runtime.provider_executables import ResolvedExecutable


def _descriptor(path: Path) -> ResolvedExecutable:
    return ResolvedExecutable(
        path.resolve(),
        "codex-cli 0.154.0",
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _request(tmp_path: Path) -> AgentRequest:
    view, scratch = tmp_path / "view", tmp_path / "scratch"
    project = tmp_path / "canonical-project"
    view.mkdir()
    scratch.mkdir()
    project.mkdir()
    return AgentRequest(
        "run-7",
        "worker-7",
        AgentRole.FORMALIZER,
        "fixture",
        None,
        view,
        scratch,
        "session-7",
        project / ".aizim" / "run" / "gateway.sock",
        30.0,
    )


def _sandbox(request: AgentRequest, executable: Path) -> SandboxLaunchSpec:
    return compile_macos_profile(
        executable,
        SandboxRequest(
            request.gateway_broker_socket.parents[2],
            request.view_root,
            request.scratch_root,
            ("/usr/bin/true",),
            {"PATH": "/usr/bin"},
        ),
        _developer_root(request),
    )


def _developer_root(request: AgentRequest) -> Path:
    return request.view_root.parent / "approved-developer-root"


def test_parent_owned_result_path_is_outside_model_writable_scratch(tmp_path: Path) -> None:
    request = _request(tmp_path)
    executable = tmp_path / "codex"
    executable.write_text("fixture")

    spec = build_codex_launch_spec(
        request,
        _sandbox(request, executable),
        Path("/opt/aizim/bin/aizim-gateway-sidecar"),
    )

    assert spec.final_message_path.parent == request.view_root
    assert not spec.final_message_path.is_relative_to(request.scratch_root)


@pytest.mark.parametrize("weakening", ("bypass", "approval", "network"))
def test_launch_spec_rejects_a_weakened_compiled_profile(tmp_path: Path, weakening: str) -> None:
    request = _request(tmp_path)
    executable = tmp_path / "codex"
    executable.write_text("fixture")
    sandbox = _sandbox(request, executable)
    if weakening == "bypass":
        argv = (sandbox.argv[0], "--dangerously-bypass-approvals-and-sandbox", *sandbox.argv[1:])
    elif weakening == "approval":
        argv = tuple(
            'approval_policy="on-request"' if value == 'approval_policy="never"' else value
            for value in sandbox.argv
        )
    else:
        argv = tuple(
            value.replace("network={enabled=false}", "network={enabled=true}")
            for value in sandbox.argv
        )
        overrides = argv[2:9:2]
        policy_hash = hashlib.sha256(
            json.dumps(overrides, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        sandbox = replace(sandbox, policy_hash=policy_hash)

    with pytest.raises(ValueError, match="invalid macOS sandbox profile"):
        validate_macos_profile(
            replace(sandbox, argv=argv),
            request.gateway_broker_socket.parents[2],
            _developer_root(request),
        )


async def test_launcher_rejects_a_dangling_result_symlink_before_spawn(tmp_path: Path) -> None:
    request = _request(tmp_path)
    executable = tmp_path / "codex"
    executable.write_text("fixture")
    spec = build_codex_launch_spec(
        request,
        _sandbox(request, executable),
        Path("/opt/aizim/bin/aizim-gateway-sidecar"),
    )
    outside = tmp_path / "outside"
    os.symlink(outside, spec.final_message_path)

    with pytest.raises(AgentLaunchError, match="CODEX_RESULT_PATH_OCCUPIED"):
        await launch_codex(replace(spec, argv=("/usr/bin/true",)))
    assert not outside.exists()


async def test_repeated_cancellation_cannot_skip_revoke_or_cleanup(tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    executable.write_text("fixture")
    request = _request(tmp_path)
    sandbox = _sandbox(request, executable)
    launch_started, revoke_started, release_revoke = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    actions: list[str] = []

    async def launch(_spec) -> CodexLaunchOutcome:
        launch_started.set()
        await asyncio.Event().wait()
        raise AssertionError

    async def revoke(_request: AgentRequest) -> None:
        actions.append("revoke-start")
        revoke_started.set()
        await release_revoke.wait()
        actions.append("revoke-done")

    async def cleanup(_request: AgentRequest) -> None:
        actions.append("cleanup")

    backend = CodexBackend(
        CodexBackendDependencies(
            _descriptor(executable),
            lambda _path: "codex-cli 0.154.0",
            lambda _request: replace(sandbox),
            Path("/opt/aizim/bin/aizim-gateway-sidecar"),
            launch,
            revoke,
            cleanup,
        )
    )
    run = asyncio.create_task(backend.run(request))
    await launch_started.wait()
    run.cancel("first")
    await revoke_started.wait()
    run.cancel("second")
    await asyncio.sleep(0)
    assert not run.done()
    release_revoke.set()
    with pytest.raises(asyncio.CancelledError, match="first"):
        await run
    assert actions == ["revoke-start", "revoke-done", "cleanup"]


async def test_replaced_codex_is_rejected_before_the_sandbox_compiler(tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    executable.write_text("fixture")
    request = _request(tmp_path)
    sandbox = _sandbox(request, executable)
    compiled = False

    def compile_sandbox(_request: AgentRequest) -> SandboxLaunchSpec:
        nonlocal compiled
        compiled = True
        return sandbox

    async def unreachable_launch(_spec) -> CodexLaunchOutcome:
        raise AssertionError

    async def finalize(_request: AgentRequest) -> None:
        return None

    backend = CodexBackend(
        CodexBackendDependencies(
            _descriptor(executable),
            lambda _path: "codex-cli 0.154.0",
            compile_sandbox,
            Path("/opt/aizim/bin/aizim-gateway-sidecar"),
            unreachable_launch,
            finalize,
            finalize,
        )
    )
    executable.write_text("replacement")

    with pytest.raises(CodexBackendError, match="CODEX_IMAGE_CHANGED"):
        await backend.run(request)
    assert not compiled


@pytest.mark.parametrize("binding", ("project", "developer"))
def test_launch_spec_rejects_profile_roots_outside_authorized_bindings(
    tmp_path: Path, binding: str
) -> None:
    request = _request(tmp_path)
    executable = tmp_path / "codex"
    executable.write_text("fixture")
    developer_root = _developer_root(request)
    sandbox = compile_macos_profile(
        executable,
        SandboxRequest(
            request.gateway_broker_socket.parents[2],
            request.view_root,
            request.scratch_root,
            ("/usr/bin/true",),
            {"PATH": "/usr/bin"},
        ),
        developer_root,
    )
    expected = request.gateway_broker_socket.parents[2] if binding == "project" else developer_root
    replacement = tmp_path / f"unapproved-{binding}"
    argv = tuple(value.replace(str(expected), str(replacement)) for value in sandbox.argv)
    overrides = argv[2:9:2]
    policy_hash = hashlib.sha256(
        json.dumps(overrides, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()

    with pytest.raises(ValueError, match="invalid macOS sandbox profile"):
        validate_macos_profile(
            replace(sandbox, argv=argv, policy_hash=policy_hash),
            request.gateway_broker_socket.parents[2],
            developer_root,
        )
