from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from aizim.agents.macos_sandbox import MacOSSandboxAdapter, SandboxHostError
from aizim.agents.platform_sandbox import sandbox_adapter
from aizim.agents.sandbox import (
    SandboxLaunchSpec,
    SandboxRequest,
    validate_launch_spec,
)


def request(tmp_path: Path) -> SandboxRequest:
    project = tmp_path / "project"
    view = tmp_path / "aizim-view-platform"
    scratch = tmp_path / "aizim-scratch-platform"
    for path in (project, view, scratch):
        path.mkdir()
    return SandboxRequest(
        project.resolve(),
        view.resolve(),
        scratch.resolve(),
        ("/usr/bin/true",),
        {},
    )


def launch_spec(sandbox_request: SandboxRequest) -> SandboxLaunchSpec:
    return SandboxLaunchSpec(
        platform_id="darwin",
        argv=("/opt/aizim/codex", *sandbox_request.command),
        cwd=sandbox_request.view_root,
        parent_env={},
        shell_env={},
        view_root=sandbox_request.view_root,
        scratch_root=sandbox_request.scratch_root,
        profile_id="aizim-worker",
        policy_hash="a" * 64,
    )


def test_shared_request_defaults_runtime_roots_and_validates_a_closed_spec(
    tmp_path: Path,
) -> None:
    sandbox_request = request(tmp_path)
    spec = launch_spec(sandbox_request)

    assert sandbox_request.runtime_read_roots == ()
    validate_launch_spec(sandbox_request, spec)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("platform_id", "windows"),
        ("cwd", Path("/wrong")),
        ("view_root", Path("/wrong")),
        ("scratch_root", Path("/wrong")),
        ("profile_id", ""),
        ("policy_hash", "short"),
        ("argv", ("/opt/aizim/codex", "/usr/bin/false")),
    ],
)
def test_shared_validator_rejects_every_generic_mismatch(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    sandbox_request = request(tmp_path)
    spec = replace(launch_spec(sandbox_request), **{field: value})

    with pytest.raises(ValueError, match="invalid sandbox launch specification"):
        validate_launch_spec(sandbox_request, spec)


def test_platform_selector_uses_the_supplied_native_codex(tmp_path: Path) -> None:
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o755)

    adapter = sandbox_adapter(codex.resolve(), "darwin")

    assert isinstance(adapter, MacOSSandboxAdapter)
    assert adapter.codex_executable == codex.resolve()
    assert adapter.platform_id == "darwin"


def test_platform_selector_rejects_unsupported_hosts(tmp_path: Path) -> None:
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o755)

    with pytest.raises(SandboxHostError, match="unsupported sandbox platform"):
        sandbox_adapter(codex.resolve(), "win32")
