from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from aizim.agents.macos_sandbox import (
    MacOSSandboxAdapter,
    MacOSSandboxDependencies,
    SandboxHostError,
)
from aizim.agents.sandbox import SandboxRequest

_EPHEMERAL_ROOTS: set[Path] = set()


@pytest.fixture(autouse=True)
def cleanup_ephemeral_roots() -> Iterator[None]:
    yield
    for root in _EPHEMERAL_ROOTS:
        shutil.rmtree(root, ignore_errors=True)
    _EPHEMERAL_ROOTS.clear()


def roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    tmp_path.chmod(0o700)
    project = tmp_path / 'project "quoted"'
    project.mkdir(parents=True)
    view = tmp_path / "aizim-view-profile"
    scratch = tmp_path / "aizim-scratch-profile"
    view.mkdir(mode=0o700)
    scratch.mkdir(mode=0o700)
    _EPHEMERAL_ROOTS.update((view, scratch))
    return project.resolve(), view.resolve(), scratch.resolve()


def dependencies(
    *,
    platform: str = "darwin",
    codex_version: Callable[[], str] = lambda: "codex-cli 0.144.6",
    sandbox_is_apple: Callable[[], bool] = lambda: True,
    developer_root: Callable[[], Path] = lambda: Path("/Library/Developer/CommandLineTools"),
) -> MacOSSandboxDependencies:
    return MacOSSandboxDependencies(
        platform=platform,
        codex_executable=Path("/opt/homebrew/bin/codex"),
        codex_version=codex_version,
        sandbox_is_apple=sandbox_is_apple,
        developer_root=developer_root,
    )


def request(tmp_path: Path, secret: str = "do-not-render") -> SandboxRequest:
    project, view, scratch = roots(tmp_path)
    return SandboxRequest(
        project_root=project,
        view_root=view,
        scratch_root=scratch,
        command=("/usr/bin/python3", "-I", "-B", "-"),
        parent_env={"PATH": "/usr/bin", "AIZIM_TEST_TOKEN": secret},
    )


def test_profile_compiles_one_deterministic_inline_permission_table(
    tmp_path: Path,
) -> None:
    adapter = MacOSSandboxAdapter(dependencies())
    sandbox_request = request(tmp_path)

    first = adapter.compile(sandbox_request)
    second = adapter.compile(sandbox_request)

    assert first == second
    assert first.profile_id == "aizim-worker"
    assert first.cwd == sandbox_request.view_root
    assert first.shell_env == {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(sandbox_request.scratch_root),
    }
    assert len(first.policy_hash) == 64
    argv = first.argv
    assert argv.count("-c") == 4
    permission = next(value for value in argv if value.startswith("permissions.aizim-worker="))
    assert '":minimal"="read"' in permission
    assert 'project \\"quoted\\""="deny"' in permission
    assert 'CommandLineTools"="read"' in permission
    assert str(sandbox_request.view_root) in permission
    assert str(sandbox_request.scratch_root) in permission
    assert "--permission-profile" in argv
    assert "--sandbox-state-disable-network" in argv
    assert "--log-denials" in argv
    assert "--allow-unix-socket" not in argv
    assert "--strict-config" not in argv
    assert "do-not-render" not in repr(first)
    assert "do-not-render" not in repr(sandbox_request)


def test_policy_hash_changes_with_roots(tmp_path: Path) -> None:
    adapter = MacOSSandboxAdapter(dependencies())
    first = adapter.compile(request(tmp_path / "one"))
    second = adapter.compile(request(tmp_path / "two"))

    assert first.policy_hash != second.policy_hash


@pytest.mark.parametrize(
    "invalid_dependencies",
    (
        dependencies(platform="linux"),
        dependencies(codex_version=lambda: "codex-cli 0.144.5"),
        dependencies(sandbox_is_apple=lambda: False),
        dependencies(developer_root=lambda: Path("relative")),
    ),
)
def test_adapter_fails_closed_on_invalid_host(
    tmp_path: Path, invalid_dependencies: MacOSSandboxDependencies
) -> None:
    adapter = MacOSSandboxAdapter(invalid_dependencies)

    with pytest.raises(SandboxHostError):
        adapter.compile(request(tmp_path))


def test_adapter_rejects_non_ephemeral_roots(tmp_path: Path) -> None:
    project = tmp_path / "project"
    view = tmp_path / "view"
    scratch = tmp_path / "scratch"
    for path in (project, view, scratch):
        path.mkdir()
    sandbox_request = SandboxRequest(
        project.resolve(),
        view.resolve(),
        scratch.resolve(),
        ("/usr/bin/python3",),
        {},
    )

    with pytest.raises(SandboxHostError):
        MacOSSandboxAdapter(dependencies()).compile(sandbox_request)


def test_adapter_rejects_canonical_project_inside_private_tmp(tmp_path: Path) -> None:
    project = Path("/private/tmp") / f"aizim-project-{tmp_path.name}"
    project.mkdir(mode=0o700)
    _EPHEMERAL_ROOTS.add(project)
    _unused_project, view, scratch = roots(tmp_path)
    sandbox_request = SandboxRequest(
        project.resolve(),
        view,
        scratch,
        ("/usr/bin/python3",),
        {},
    )

    with pytest.raises(SandboxHostError):
        MacOSSandboxAdapter(dependencies()).compile(sandbox_request)


def test_adapter_rejects_globally_writable_temporary_roots(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path.name
    view = Path("/private/tmp") / f"aizim-view-{token}"
    scratch = Path("/private/tmp") / f"aizim-scratch-{token}"
    view.mkdir(mode=0o700)
    scratch.mkdir(mode=0o700)
    _EPHEMERAL_ROOTS.update((view, scratch))

    with pytest.raises(SandboxHostError):
        MacOSSandboxAdapter(dependencies()).compile(
            SandboxRequest(project, view, scratch, ("/usr/bin/python3",), {})
        )


def test_adapter_rejects_canonical_project_inside_private_var_tmp(tmp_path: Path) -> None:
    project = Path("/private/var/tmp") / f"aizim-project-{tmp_path.name}"
    project.mkdir(mode=0o700)
    _EPHEMERAL_ROOTS.add(project)
    _unused_project, view, scratch = roots(tmp_path)

    with pytest.raises(SandboxHostError):
        MacOSSandboxAdapter(dependencies()).compile(
            SandboxRequest(project, view, scratch, ("/usr/bin/python3",), {})
        )
