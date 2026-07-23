from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from aizim.agents.linux_sandbox import (
    LinuxSandboxAdapter,
    LinuxSandboxDependencies,
)
from aizim.agents.macos_sandbox import SandboxHostError
from aizim.agents.platform_sandbox import sandbox_adapter
from aizim.agents.sandbox import SandboxRequest


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path.resolve()


def host_tree(tmp_path: Path) -> tuple[Path, Path]:
    triple = tmp_path / "vendor" / "x86_64-unknown-linux-musl"
    codex = executable(triple / "bin" / "codex")
    bwrap = executable(triple / "codex-resources" / "bwrap")
    return codex, bwrap


def dependencies(
    codex: Path,
    bwrap: Path,
    *,
    platform: str = "linux",
    version: str = "codex-cli 0.145.0",
    usable: bool = True,
) -> LinuxSandboxDependencies:
    return LinuxSandboxDependencies(
        platform=platform,
        codex_executable=codex,
        codex_version=lambda: version,
        bundled_bwrap=lambda: bwrap,
        bwrap_usable=lambda _path: usable,
    )


def request(tmp_path: Path) -> SandboxRequest:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    project = parent / "project"
    view = parent / "aizim-view-linux"
    scratch = parent / "aizim-scratch-linux"
    runtime = parent / "runtime"
    for path in (project, view, scratch, runtime):
        path.mkdir(mode=0o700)
    return SandboxRequest(
        project.resolve(),
        view.resolve(),
        scratch.resolve(),
        ("/usr/bin/true",),
        {},
        (runtime.resolve(),),
    )


def test_linux_adapter_compiles_only_after_all_host_checks(tmp_path: Path) -> None:
    codex, bwrap = host_tree(tmp_path)
    adapter = LinuxSandboxAdapter(dependencies(codex, bwrap))

    spec = adapter.compile(request(tmp_path))

    assert adapter.platform_id == "linux"
    assert adapter.codex_executable == codex
    assert adapter.sandbox_executable == bwrap
    assert spec.platform_id == "linux"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: replace(value, platform="darwin"),
        lambda value: replace(value, codex_version=lambda: "codex-cli 0.144.0"),
        lambda value: replace(value, bundled_bwrap=lambda: Path("/other/bwrap")),
        lambda value: replace(value, bwrap_usable=lambda _path: False),
    ],
)
def test_linux_adapter_fails_closed_on_invalid_host_dependencies(
    tmp_path: Path,
    mutator: Callable[[LinuxSandboxDependencies], LinuxSandboxDependencies],
) -> None:
    codex, bwrap = host_tree(tmp_path)
    adapter = LinuxSandboxAdapter(mutator(dependencies(codex, bwrap)))

    with pytest.raises(SandboxHostError):
        adapter.compile(request(tmp_path))


def test_linux_adapter_rejects_non_executable_codex_and_bwrap(tmp_path: Path) -> None:
    codex, bwrap = host_tree(tmp_path)
    sandbox_request = request(tmp_path)

    for path in (codex, bwrap):
        path.chmod(0o644)
        with pytest.raises(SandboxHostError):
            LinuxSandboxAdapter(dependencies(codex, bwrap)).compile(sandbox_request)
        path.chmod(0o755)


def test_platform_selector_returns_linux_adapter_for_packaged_codex(
    tmp_path: Path,
) -> None:
    codex, _bwrap = host_tree(tmp_path)

    adapter = sandbox_adapter(codex, "linux")

    assert isinstance(adapter, LinuxSandboxAdapter)
    assert adapter.codex_executable == codex
