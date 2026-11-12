from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from aizim.agents.macos_sandbox import (
    MacOSSandboxAdapter,
    MacOSSandboxDependencies,
    SandboxHostError,
)
from aizim.agents.permission_profile import normalize_sandbox_request, sandbox_environment
from aizim.agents.sandbox import ProviderEnvironmentPolicy, SandboxRequest
from aizim.omp.runtime import OMP_VERSION


def request(tmp_path: Path) -> SandboxRequest:
    tmp_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_path.chmod(0o700)
    project = tmp_path / "project"
    view = tmp_path / "aizim-view-profile"
    scratch = tmp_path / "aizim-scratch-profile"
    for path in (project, view, scratch):
        path.mkdir(mode=0o700)
    return SandboxRequest(project.resolve(), view.resolve(), scratch.resolve(),
                          ("/usr/bin/python3", "-I", "-B", "-"), {})


def dependencies(developer: Path, *, platform: str = "darwin",
                 version: Callable[[], str] = lambda: f"omp/{OMP_VERSION}",
                 signed: Callable[[], bool] = lambda: True) -> MacOSSandboxDependencies:
    agent = developer / "omp"
    agent.write_text("#!/bin/sh\n")
    agent.chmod(0o755)
    return MacOSSandboxDependencies(platform, agent.resolve(), version,
                                    signed, lambda: developer)


def test_macos_adapter_compiles_deterministically_and_preserves_command(tmp_path: Path) -> None:
    sandbox_request = request(tmp_path)
    adapter = MacOSSandboxAdapter(dependencies(tmp_path))
    first = adapter.compile(sandbox_request)
    second = adapter.compile(sandbox_request)

    assert first == second
    assert first.platform_id == "darwin"
    assert first.cwd == sandbox_request.view_root
    assert first.argv[-len(sandbox_request.command):] == sandbox_request.command
    assert len(first.policy_hash) == 64
    assert first.parent_env == first.shell_env


@pytest.mark.parametrize("kind", ("platform", "version", "signature", "developer"))
def test_macos_adapter_fails_closed_on_invalid_host(tmp_path: Path, kind: str) -> None:
    sandbox_request = request(tmp_path)
    host = dependencies(tmp_path)
    if kind == "platform":
        host = replace(host, platform="linux")
    elif kind == "version":
        host = replace(host, agent_version=lambda: "omp/unsupported")
    elif kind == "signature":
        host = replace(host, sandbox_is_apple=lambda: False)
    else:
        host = replace(host, developer_root=lambda: Path("relative"))
    with pytest.raises(SandboxHostError):
        MacOSSandboxAdapter(host).compile(sandbox_request)


@pytest.mark.parametrize("kind", ("missing", "relative", "symlink", "directory", "nonexecutable"))
def test_macos_adapter_rejects_invalid_agent_executable(tmp_path: Path, kind: str) -> None:
    sandbox_request = request(tmp_path)
    host = dependencies(tmp_path)
    executable = host.agent_executable
    if kind == "missing":
        executable.unlink()
    elif kind == "relative":
        executable = Path("omp")
    elif kind == "symlink":
        alias = tmp_path / "omp-link"
        alias.symlink_to(executable)
        executable = alias
    elif kind == "directory":
        executable = tmp_path
    else:
        executable.chmod(0o644)

    with pytest.raises(SandboxHostError):
        MacOSSandboxAdapter(replace(host, agent_executable=executable)).compile(sandbox_request)


@pytest.mark.parametrize("kind", ("relative", "symlink", "project", "view", "scratch"))
def test_runtime_roots_reject_noncanonical_or_overlapping_paths(tmp_path: Path, kind: str) -> None:
    sandbox_request = request(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    link = tmp_path / "runtime-link"
    link.symlink_to(runtime, target_is_directory=True)
    candidates = {"relative": Path("relative"), "symlink": link,
                  "project": sandbox_request.project_root, "view": sandbox_request.view_root,
                  "scratch": sandbox_request.scratch_root}
    with pytest.raises(ValueError):
        normalize_sandbox_request(replace(sandbox_request, runtime_read_roots=(candidates[kind],)))


def test_sandbox_environment_strips_credentials_and_uses_scratch_tmp(tmp_path: Path) -> None:
    sandbox_request = request(tmp_path)
    secret = "provider-secret-value"
    environment = {"PATH": "/custom/bin", "HOME": "/isolated/home", "OPENAI_API_KEY": secret,
                   "ANTHROPIC_API_KEY": secret, "AIZIM_TOKEN": secret, "TMPDIR": "/host/tmp"}
    filtered = replace(sandbox_request, parent_env=environment,
                       provider_environment=ProviderEnvironmentPolicy.FILTERED_PARENT)
    env = sandbox_environment(normalize_sandbox_request(filtered))

    assert env == {"PATH": "/custom/bin", "HOME": "/isolated/home", "LANG": "C.UTF-8",
                   "LC_ALL": "C.UTF-8", "TMPDIR": str(sandbox_request.scratch_root)}
    assert secret not in repr(filtered)
    assert secret not in repr(env)


def test_sandbox_rejects_claimed_network_destination_restrictions(tmp_path: Path) -> None:
    sandbox_request = request(tmp_path)
    with pytest.raises(ValueError, match="destination network restrictions are not supported"):
        normalize_sandbox_request(
            replace(sandbox_request, provider_network_domains=("example.com",))
        )


def test_sandbox_rejects_non_private_ephemeral_roots(tmp_path: Path) -> None:
    sandbox_request = request(tmp_path)
    sandbox_request.view_root.chmod(0o755)
    with pytest.raises(ValueError, match="private temporary parent"):
        normalize_sandbox_request(sandbox_request)
