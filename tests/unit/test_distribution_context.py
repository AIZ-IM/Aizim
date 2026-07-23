from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import aizim.runtime.distribution as distribution
from aizim.runtime.distribution import (
    DISTRIBUTION_ENVIRONMENT,
    DistributionError,
    load_distribution_context,
    resolve_codex_executable,
    resolve_ripgrep_executable,
    without_distribution_environment,
)


def _executable(path: Path, version: str = "codex-cli 0.145.0") -> Path:
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
    path.chmod(0o755)
    return path


def _npm_environment(executable: Path) -> dict[str, str]:
    return {
        "AIZIM_DISTRIBUTION_MODE": "npm",
        "AIZIM_DISTRIBUTION_VERSION": "0.1.0",
        "AIZIM_DISTRIBUTION_TARGET": "darwin-arm64",
        "AIZIM_CODEX_EXECUTABLE": str(executable),
        "AIZIM_DISTRIBUTION_MANIFEST_SHA256": "1" * 64,
        "AIZIM_PLATFORM_MANIFEST_SHA256": "2" * 64,
    }


def test_no_distribution_keys_selects_source_mode() -> None:
    context = load_distribution_context({"PATH": "/usr/bin"})

    assert context.mode == "source"
    assert context.version == "0.1.0"
    assert context.target is None
    assert context.codex_executable is None


def test_all_exact_keys_produce_an_immutable_path_safe_npm_context(tmp_path: Path) -> None:
    codex = _executable(tmp_path / "codex")
    context = load_distribution_context(_npm_environment(codex))

    assert context.mode == "npm"
    assert context.target == "darwin-arm64"
    assert context.codex_executable == codex
    assert context.distribution_manifest_sha256 == "1" * 64
    assert context.platform_manifest_sha256 == "2" * 64
    assert str(tmp_path) not in repr(context)
    with pytest.raises(FrozenInstanceError):
        context.__setattr__("target", "linux-x64")


def test_every_partial_distribution_environment_fails_closed(tmp_path: Path) -> None:
    environment = _npm_environment(_executable(tmp_path / "codex"))

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
    environment = _npm_environment(_executable(tmp_path / "codex"))
    environment[name] = value

    with pytest.raises(DistributionError) as failure:
        load_distribution_context(environment)

    assert failure.value.code == code
    assert str(tmp_path) not in str(failure.value)


def test_rejects_relative_missing_directory_symlink_and_non_executable_paths(
    tmp_path: Path,
) -> None:
    regular = _executable(tmp_path / "regular")
    directory = tmp_path / "directory"
    directory.mkdir()
    missing = tmp_path / "missing"
    symlink = tmp_path / "symlink"
    symlink.symlink_to(regular)
    non_executable = tmp_path / "non-executable"
    non_executable.write_text("not executable\n")

    for candidate in [
        Path("relative-codex"),
        missing,
        directory,
        symlink,
        non_executable,
    ]:
        environment = _npm_environment(regular)
        environment["AIZIM_CODEX_EXECUTABLE"] = str(candidate)
        with pytest.raises(DistributionError) as failure:
            load_distribution_context(environment)
        assert failure.value.code == "CODEX_EXECUTABLE_INVALID"
        assert str(candidate) not in str(failure.value)


def test_npm_resolution_ignores_path_and_source_resolution_uses_it(tmp_path: Path) -> None:
    npm_codex = _executable(tmp_path / "npm-codex")
    path_root = tmp_path / "bin"
    path_root.mkdir()
    path_codex = _executable(path_root / "codex")
    npm_environment = _npm_environment(npm_codex)
    npm_environment["PATH"] = str(path_root)

    assert resolve_codex_executable(npm_environment) == npm_codex
    assert resolve_codex_executable({"PATH": str(path_root)}) == path_codex


def test_npm_resolution_uses_ripgrep_from_the_verified_codex_bundle(tmp_path: Path) -> None:
    triple = tmp_path / "codex-native" / "vendor" / "aarch64-apple-darwin"
    (triple / "bin").mkdir(parents=True)
    (triple / "codex-path").mkdir()
    codex = _executable(triple / "bin" / "codex")
    ripgrep = _executable(triple / "codex-path" / "rg")
    environment = _npm_environment(codex)
    environment["PATH"] = ""

    assert resolve_ripgrep_executable(environment) == ripgrep


def test_source_resolution_uses_ripgrep_from_the_codex_native_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = tmp_path / "codex"
    (meta / "bin").mkdir(parents=True)
    codex = _executable(meta / "bin" / "codex")
    triple = (
        meta
        / "node_modules"
        / "@openai"
        / "codex-darwin-arm64"
        / "vendor"
        / "aarch64-apple-darwin"
    )
    (triple / "codex-path").mkdir(parents=True)
    ripgrep = _executable(triple / "codex-path" / "rg")
    monkeypatch.setattr(
        distribution,
        "_host_codex_layout",
        lambda: ("darwin-arm64", "aarch64-apple-darwin"),
    )

    assert resolve_ripgrep_executable({"PATH": str(codex.parent)}) == ripgrep


def test_source_resolution_rejects_an_unavailable_codex() -> None:
    with pytest.raises(DistributionError) as failure:
        resolve_codex_executable({"PATH": ""})

    assert failure.value.code == "CODEX_EXECUTABLE_UNAVAILABLE"


def test_without_distribution_environment_removes_only_the_six_internal_keys(
    tmp_path: Path,
) -> None:
    environment = _npm_environment(_executable(tmp_path / "codex"))
    environment.update(PATH="/usr/bin", LANG="C.UTF-8")

    scrubbed = without_distribution_environment(environment)

    assert DISTRIBUTION_ENVIRONMENT.isdisjoint(scrubbed)
    assert scrubbed == {"PATH": "/usr/bin", "LANG": "C.UTF-8"}
    assert environment["AIZIM_DISTRIBUTION_MODE"] == "npm"
