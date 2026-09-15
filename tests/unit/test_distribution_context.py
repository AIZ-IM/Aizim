from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import aizim.runtime.distribution as distribution
import aizim.runtime.provider_executables as providers
from aizim.runtime.distribution import (
    DISTRIBUTION_ENVIRONMENT,
    DistributionError,
    load_distribution_context,
    resolve_ripgrep_executable,
    without_distribution_environment,
)


def _executable(path: Path, version: str = "codex-cli 0.154.0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
    path.chmod(0o755)
    return path


def _npm_environment() -> dict[str, str]:
    return {
        "AIZIM_DISTRIBUTION_MODE": "npm",
        "AIZIM_DISTRIBUTION_VERSION": "0.1.0",
        "AIZIM_DISTRIBUTION_TARGET": "darwin-arm64",
        "AIZIM_DISTRIBUTION_MANIFEST_SHA256": "1" * 64,
        "AIZIM_PLATFORM_MANIFEST_SHA256": "2" * 64,
    }


def test_no_distribution_keys_selects_source_mode() -> None:
    context = load_distribution_context({"PATH": "/usr/bin"})

    assert context.mode == "source"
    assert context.version == "0.1.0"
    assert context.target is None
    assert not hasattr(context, "claude_executable")
    assert not hasattr(context, "codex_executable")


def test_distribution_boundary_exposes_no_provider_specific_resolvers() -> None:
    assert not hasattr(distribution, "resolve_codex_executable")
    assert not hasattr(distribution, "resolve_claude_executable")


def test_all_exact_keys_produce_an_immutable_path_safe_npm_context(tmp_path: Path) -> None:
    context = load_distribution_context(_npm_environment())

    assert context.mode == "npm"
    assert context.target == "darwin-arm64"
    assert context.distribution_manifest_sha256 == "1" * 64
    assert context.platform_manifest_sha256 == "2" * 64
    assert str(tmp_path) not in repr(context)
    with pytest.raises(FrozenInstanceError):
        context.__setattr__("target", "linux-x64")


def test_every_partial_distribution_environment_fails_closed(tmp_path: Path) -> None:
    environment = _npm_environment()

    for missing in DISTRIBUTION_ENVIRONMENT:
        partial = dict(environment)
        partial.pop(missing)
        with pytest.raises(DistributionError) as failure:
            load_distribution_context(partial)
        assert failure.value.code == "DISTRIBUTION_ENVIRONMENT_INVALID"


@pytest.mark.parametrize(
    ("name", "value", "code"),
    [
        ("AIZIM_DISTRIBUTION_MODE", "source", "DISTRIBUTION_MODE_INVALID"),
        ("AIZIM_DISTRIBUTION_VERSION", "0.2.0", "DISTRIBUTION_VERSION_INVALID"),
        ("AIZIM_DISTRIBUTION_TARGET", "win32-x64", "DISTRIBUTION_TARGET_INVALID"),
        (
            "AIZIM_DISTRIBUTION_MANIFEST_SHA256",
            "A" * 64,
            "DISTRIBUTION_DIGEST_INVALID",
        ),
        (
            "AIZIM_PLATFORM_MANIFEST_SHA256",
            "2" * 63,
            "DISTRIBUTION_DIGEST_INVALID",
        ),
    ],
)
def test_invalid_closed_values_have_stable_safe_codes(
    tmp_path: Path,
    name: str,
    value: str,
    code: str,
) -> None:
    environment = _npm_environment()
    environment[name] = value

    with pytest.raises(DistributionError) as failure:
        load_distribution_context(environment)

    assert failure.value.code == code
    assert str(tmp_path) not in str(failure.value)


def test_external_resolvers_reject_relative_missing_directory_and_non_executable_paths(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    missing = tmp_path / "missing"
    non_executable = tmp_path / "non-executable"
    non_executable.write_text("not executable\n")

    for name, code in [
        ("AIZIM_CODEX_EXECUTABLE", "CODEX_EXECUTABLE_UNAVAILABLE"),
        ("AIZIM_CLAUDE_EXECUTABLE", "CLAUDE_EXECUTABLE_UNAVAILABLE"),
    ]:
        for candidate in [
            Path("relative-provider"),
            missing,
            directory,
            non_executable,
        ]:
            environment = {name: str(candidate), "PATH": ""}
            with pytest.raises(providers.ProviderExecutableError) as failure:
                resolver = (
                    providers.discover_codex_executable
                    if name == "AIZIM_CODEX_EXECUTABLE"
                    else providers.discover_claude_executable
                )
                resolver(environment)
            assert failure.value.code == code
            assert str(candidate) not in str(failure.value)


def test_npm_mode_uses_independent_external_overrides_before_path(tmp_path: Path) -> None:
    override_codex = _executable(tmp_path / "override-codex")
    override_claude = _executable(
        tmp_path / "override-claude",
        "2.1.218 (Claude Code)",
    )
    path_root = tmp_path / "bin"
    path_codex = _executable(path_root / "codex")
    path_claude = _executable(path_root / "claude", "2.1.218 (Claude Code)")
    environment = {
        **_npm_environment(),
        "AIZIM_CODEX_EXECUTABLE": str(override_codex),
        "AIZIM_CLAUDE_EXECUTABLE": str(override_claude),
        "PATH": str(path_root),
    }

    assert providers.discover_codex_executable(environment) == override_codex
    assert providers.discover_claude_executable(environment) == override_claude
    assert providers.discover_codex_executable({"PATH": str(path_root)}) == path_codex
    assert providers.discover_claude_executable({"PATH": str(path_root)}) == path_claude


def test_ripgrep_fallback_is_distribution_mode_independent(tmp_path: Path) -> None:
    triple = tmp_path / "codex-native" / "vendor" / "aarch64-apple-darwin"
    (triple / "bin").mkdir(parents=True)
    (triple / "codex-path").mkdir()
    codex = _executable(triple / "bin" / "codex")
    ripgrep = _executable(triple / "codex-path" / "rg")
    environment = {
        **_npm_environment(),
        "AIZIM_CODEX_EXECUTABLE": str(codex),
        "PATH": "",
    }

    assert resolve_ripgrep_executable(environment) == ripgrep


def test_source_resolution_uses_ripgrep_from_the_codex_native_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = tmp_path / "codex"
    codex = _executable(meta / "bin" / "codex.js")
    (meta / "bin" / "codex").symlink_to(codex)
    (meta / "package.json").write_text('{"name":"@openai/codex"}\n')
    triple = (
        meta / "node_modules" / "@openai" / "codex-darwin-arm64" / "vendor" / "aarch64-apple-darwin"
    )
    (triple / "codex-path").mkdir(parents=True)
    ripgrep = _executable(triple / "codex-path" / "rg")
    monkeypatch.setattr(
        providers,
        "_host_codex_layout",
        lambda: ("darwin-arm64", "aarch64-apple-darwin"),
    )

    assert resolve_ripgrep_executable({"PATH": str(codex.parent)}) == ripgrep


def test_source_resolution_rejects_an_unavailable_codex() -> None:
    with pytest.raises(providers.ProviderExecutableError) as failure:
        providers.discover_codex_executable({"PATH": ""})

    assert failure.value.code == "CODEX_EXECUTABLE_UNAVAILABLE"

    with pytest.raises(providers.ProviderExecutableError) as claude_failure:
        providers.discover_claude_executable({"PATH": ""})

    assert claude_failure.value.code == "CLAUDE_EXECUTABLE_UNAVAILABLE"


def test_without_distribution_environment_removes_only_internal_keys(
    tmp_path: Path,
) -> None:
    environment = {
        **_npm_environment(),
        "AIZIM_CODEX_EXECUTABLE": "/external/codex",
        "AIZIM_CLAUDE_EXECUTABLE": "/external/claude",
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
    }

    scrubbed = without_distribution_environment(environment)

    assert DISTRIBUTION_ENVIRONMENT.isdisjoint(scrubbed)
    assert scrubbed == {
        "AIZIM_CLAUDE_EXECUTABLE": "/external/claude",
        "AIZIM_CODEX_EXECUTABLE": "/external/codex",
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
    }
    assert environment["AIZIM_DISTRIBUTION_MODE"] == "npm"
