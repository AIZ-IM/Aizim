from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from aizim.agents.linux_sandbox import LinuxSandboxAdapter, LinuxSandboxDependencies
from aizim.agents.macos_sandbox import SandboxHostError
from aizim.agents.platform_sandbox import sandbox_adapter
from aizim.agents.sandbox import SandboxRequest
from aizim.omp.runtime import OMP_VERSION


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    path.chmod(0o755)
    return path.resolve()


def dependencies(agent: Path, bwrap: Path) -> LinuxSandboxDependencies:
    return LinuxSandboxDependencies(
        platform="linux",
        agent_executable=agent,
        agent_version=lambda: f"omp/{OMP_VERSION}",
        system_bwrap=lambda: bwrap,
        bwrap_usable=lambda _path: True,
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
        project.resolve(), view.resolve(), scratch.resolve(),
        ("/usr/bin/true",), {}, (runtime.resolve(),),
    )


def test_linux_adapter_launches_system_bwrap_directly(tmp_path: Path) -> None:
    agent = executable(tmp_path / "agent" / "omp")
    bwrap = executable(tmp_path / "system" / "bwrap")
    adapter = LinuxSandboxAdapter(dependencies(agent, bwrap))
    sandbox_request = request(tmp_path)

    spec = adapter.compile(sandbox_request)

    assert adapter.platform_id == spec.platform_id == "linux"
    assert adapter.sandbox_executable == bwrap
    assert spec.argv[0] == str(bwrap)
    assert str(agent) not in spec.argv
    assert spec.argv[-2:] == ("--", "/usr/bin/true")


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: replace(value, platform="darwin"),
        lambda value: replace(value, agent_version=lambda: "omp/unsupported"),
        lambda value: replace(value, system_bwrap=lambda: Path("bwrap")),
        lambda value: replace(value, system_bwrap=lambda: Path("/missing/bwrap")),
        lambda value: replace(value, bwrap_usable=lambda _path: False),
        lambda value: replace(value, agent_executable=Path("omp")),
    ],
)
def test_linux_adapter_fails_closed_on_invalid_host_dependencies(
    tmp_path: Path,
    mutator: Callable[[LinuxSandboxDependencies], LinuxSandboxDependencies],
) -> None:
    agent = executable(tmp_path / "omp")
    bwrap = executable(tmp_path / "bwrap")
    adapter = LinuxSandboxAdapter(mutator(dependencies(agent, bwrap)))

    with pytest.raises(SandboxHostError):
        adapter.compile(request(tmp_path))


@pytest.mark.parametrize("kind", ("agent", "bwrap"))
@pytest.mark.parametrize("invalid", ("nonexecutable", "symlink", "directory"))
def test_linux_adapter_requires_real_executable_files(
    tmp_path: Path, kind: str, invalid: str,
) -> None:
    agent = executable(tmp_path / "omp")
    bwrap = executable(tmp_path / "bwrap")
    target = agent if kind == "agent" else bwrap
    if invalid == "nonexecutable":
        target.chmod(0o644)
    elif invalid == "symlink":
        alias = tmp_path / "executable-link"
        alias.symlink_to(target)
        if kind == "agent":
            agent = alias
        else:
            bwrap = alias
    else:
        directory = tmp_path / "executable-directory"
        directory.mkdir()
        if kind == "agent":
            agent = directory
        else:
            bwrap = directory

    with pytest.raises(SandboxHostError):
        LinuxSandboxAdapter(dependencies(agent, bwrap)).compile(request(tmp_path))


def test_platform_selector_resolves_system_bwrap_independently_of_agent_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = executable(tmp_path / "standalone" / "omp")
    bwrap = executable(tmp_path / "system" / "bwrap")
    lookups: list[tuple[str, str]] = []

    def which(name: str, *, path: str) -> str:
        lookups.append((name, path))
        return str(bwrap)

    monkeypatch.setattr("aizim.agents.linux_sandbox.shutil.which", which)

    adapter = sandbox_adapter(agent, "linux")

    assert isinstance(adapter, LinuxSandboxAdapter)
    assert adapter.sandbox_executable == bwrap
    assert lookups == [("bwrap", os.defpath)]
