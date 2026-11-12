from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import aizim.runtime.distribution as distribution
from aizim.runtime.distribution import (
    DISTRIBUTION_ENVIRONMENT,
    DistributionError,
    load_distribution_context,
    resolve_ripgrep_executable,
    without_distribution_environment,
)


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
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


@pytest.mark.parametrize("npm_mode", [False, True])
def test_ripgrep_is_resolved_from_the_system_path(
    tmp_path: Path, npm_mode: bool
) -> None:
    ripgrep = _executable(tmp_path / "bin" / "rg")
    environment = {"PATH": str(ripgrep.parent)}
    if npm_mode:
        environment.update(_npm_environment())

    assert resolve_ripgrep_executable(environment) == ripgrep


def test_missing_ripgrep_has_a_stable_safe_error(tmp_path: Path) -> None:
    with pytest.raises(DistributionError) as failure:
        resolve_ripgrep_executable({"PATH": str(tmp_path)})

    assert failure.value.code == "RIPGREP_EXECUTABLE_UNAVAILABLE"
    assert str(tmp_path) not in str(failure.value)


def test_without_distribution_environment_removes_only_internal_keys(
    tmp_path: Path,
) -> None:
    environment = {
        **_npm_environment(),
        "AIZIM_OMP_EXECUTABLE": "/external/omp",
        "AIZIM_MAGPIE_EXECUTABLE": "/external/magpie",
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
    }

    scrubbed = without_distribution_environment(environment)

    assert DISTRIBUTION_ENVIRONMENT.isdisjoint(scrubbed)
    assert scrubbed == {
        "AIZIM_OMP_EXECUTABLE": "/external/omp",
        "AIZIM_MAGPIE_EXECUTABLE": "/external/magpie",
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
    }
    assert environment["AIZIM_DISTRIBUTION_MODE"] == "npm"
