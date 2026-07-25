from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from aizim.agents import LinuxSandboxAdapter, MacOSSandboxAdapter, sandbox_adapter
from aizim.domain import ControllerProviderId, sha256_file
from aizim.orchestration.claude_controller import ClaudeControllerBackend
from aizim.orchestration.codex_controller import CodexControllerBackend
from aizim.orchestration.codex_worker import preflight_codex_worker
from aizim.orchestration.controller_providers import (
    ResolvedControllerRuntime,
    build_controller_backend,
    resolve_controller_runtime,
)
from aizim.runtime.provider_executables import (
    CLAUDE_VERSIONS,
    CODEX_VERSIONS,
    ProviderExecutableError,
    ResolvedExecutable,
)

_ENABLED = os.environ.get("AIZIM_PROVIDER_CONTRACT") == "1"
_SELECTED = os.environ.get("AIZIM_PROVIDER_CONTRACT_PROVIDER", "all")
_PROJECT = Path(__file__).parents[2] / "examples" / "smoke_lean"
_SAFE_ENVIRONMENT = frozenset(
    {
        "AIZIM_CLAUDE_EXECUTABLE",
        "AIZIM_CODEX_EXECUTABLE",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "TMPDIR",
    }
)

pytestmark = pytest.mark.skipif(
    not _ENABLED,
    reason="set AIZIM_PROVIDER_CONTRACT=1 to test external provider CLIs",
)


def _environment(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "provider-home"
    home.mkdir()
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in _SAFE_ENVIRONMENT
    }
    environment["HOME"] = str(home)
    return environment


def _require_provider(provider: str) -> None:
    if _SELECTED not in {"all", "codex", "claude"}:
        pytest.fail(f"invalid AIZIM_PROVIDER_CONTRACT_PROVIDER: {_SELECTED}")
    if _SELECTED not in {"all", provider}:
        pytest.skip(f"{provider} is outside the selected provider contract lane")


def _assert_descriptor(
    executable: ResolvedExecutable,
    versions: frozenset[str],
) -> None:
    assert executable.path.is_absolute()
    assert executable.path == executable.path.resolve(strict=True)
    assert executable.path.is_file()
    assert not executable.path.is_symlink()
    assert executable.version in versions
    assert executable.sha256 == sha256_file(executable.path)
    assert len(executable.sha256) == 64


async def _assert_codex_worker_contract(
    project: Path,
    runtime: ResolvedControllerRuntime,
    environment: Mapping[str, str],
) -> None:
    adapter = sandbox_adapter(runtime.codex.path)
    assert isinstance(adapter, (LinuxSandboxAdapter, MacOSSandboxAdapter))
    assert adapter.codex_executable == runtime.codex.path
    assert adapter.platform_id in {"darwin", "linux"}
    await preflight_codex_worker(project, runtime.codex, environment)


async def test_codex_controller_and_worker_share_external_descriptor(
    tmp_path: Path,
) -> None:
    _require_provider("codex")
    environment = _environment(tmp_path)

    runtime = resolve_controller_runtime(ControllerProviderId("codex"), environment)
    _assert_descriptor(runtime.codex, CODEX_VERSIONS)
    assert runtime.controller is runtime.codex
    await _assert_codex_worker_contract(_PROJECT, runtime, environment)

    backend = build_controller_backend(runtime, _PROJECT, None, environment)
    assert isinstance(backend, CodexControllerBackend)
    assert backend.identity.name == "codex"
    assert backend.identity.version == runtime.controller.version
    assert backend.identity.executable_sha256 == runtime.controller.sha256


async def test_claude_controller_uses_codex_worker_sandbox_without_model_call(
    tmp_path: Path,
) -> None:
    _require_provider("claude")
    environment = _environment(tmp_path)
    try:
        runtime = resolve_controller_runtime(
            ControllerProviderId("claude"),
            environment,
        )
    except ProviderExecutableError as error:
        if _SELECTED == "claude":
            raise
        assert error.code in {
            "CLAUDE_EXECUTABLE_UNAVAILABLE",
            "CONTROLLER_VERSION_UNSUPPORTED",
        }
        if error.code == "CONTROLLER_VERSION_UNSUPPORTED":
            assert "supported='2.1.218 (Claude Code)'" in error.detail
        pytest.skip(f"local Claude contract rejected as designed: {error.code}")

    _assert_descriptor(runtime.codex, CODEX_VERSIONS)
    _assert_descriptor(runtime.controller, CLAUDE_VERSIONS)
    assert runtime.provider == ControllerProviderId("claude")
    assert runtime.controller != runtime.codex
    await _assert_codex_worker_contract(_PROJECT, runtime, environment)

    backend = build_controller_backend(runtime, _PROJECT, None, environment)
    assert isinstance(backend, ClaudeControllerBackend)
    assert backend.identity.name == "claude"
    assert backend.identity.version == runtime.controller.version
    assert backend.identity.executable_sha256 == runtime.controller.sha256
    assert backend._sandbox_descriptor == runtime.codex
