from __future__ import annotations

import os
from pathlib import Path

import pytest

import aizim.runtime.provider_executables as providers
from aizim.domain import sha256_file
from aizim.runtime.provider_executables import (
    CLAUDE_VERSIONS,
    CODEX_VERSIONS,
    ProviderExecutableError,
    codex_bwrap_executable,
    codex_ripgrep_executable,
    codex_runtime_root,
    resolve_claude,
    resolve_codex,
    revalidate_claude,
    revalidate_codex,
)


def executable(path: Path, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
    path.chmod(0o755)
    return path


def hardlinked_claude(tmp_path: Path) -> tuple[Path, Path]:
    target = executable(
        tmp_path / "lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
        "2.1.218 (Claude Code)",
    )
    native = (
        tmp_path
        / "lib/node_modules/@anthropic-ai/claude-code/node_modules"
        / "@anthropic-ai/claude-code-test/claude"
    )
    native.parent.mkdir(parents=True)
    native.hardlink_to(target)
    command = tmp_path / "bin/claude"
    command.parent.mkdir()
    command.symlink_to(target)
    return command, native


def test_resolvers_use_non_empty_override_before_path_and_remain_independent(
    tmp_path: Path,
) -> None:
    path_bin = tmp_path / "path-bin"
    path_codex = executable(path_bin / "codex", "codex-cli 0.154.0")
    path_claude = executable(path_bin / "claude", "2.1.218 (Claude Code)")
    override_codex = executable(tmp_path / "overrides/codex", "codex-cli 0.154.0")
    override_claude = executable(
        tmp_path / "overrides/claude",
        "2.1.218 (Claude Code)",
    )
    environment = {
        "PATH": str(path_bin),
        "AIZIM_CODEX_EXECUTABLE": str(override_codex),
        "AIZIM_CLAUDE_EXECUTABLE": str(override_claude),
    }

    codex = resolve_codex(environment)
    claude = resolve_claude(environment)

    assert codex.path == override_codex.resolve()
    assert claude.path == override_claude.resolve()
    assert codex.path != path_codex
    assert claude.path != path_claude


def test_empty_overrides_fall_through_to_path(tmp_path: Path) -> None:
    path_bin = tmp_path / "path-bin"
    codex = executable(path_bin / "codex", "codex-cli 0.154.0")
    claude = executable(path_bin / "claude", "2.1.218 (Claude Code)")
    environment = {
        "PATH": str(path_bin),
        "AIZIM_CODEX_EXECUTABLE": "",
        "AIZIM_CLAUDE_EXECUTABLE": "",
    }

    assert resolve_codex(environment).path == codex.resolve()
    assert resolve_claude(environment).path == claude.resolve()


def test_descriptor_contains_canonical_path_version_and_post_probe_digest(
    tmp_path: Path,
) -> None:
    target = executable(tmp_path / "real/codex", "codex-cli 0.154.0")
    alias = tmp_path / "bin/codex"
    alias.parent.mkdir()
    alias.symlink_to(target)

    descriptor = resolve_codex({"PATH": str(alias.parent)})

    assert descriptor.path == target.resolve()
    assert descriptor.version == "codex-cli 0.154.0"
    assert descriptor.sha256 == sha256_file(target)
    assert frozenset({"codex-cli 0.154.0"}) == CODEX_VERSIONS
    assert frozenset({"2.1.218 (Claude Code)"}) == CLAUDE_VERSIONS


def test_claude_resolver_accepts_official_npm_hardlink_layout(tmp_path: Path) -> None:
    command, _native = hardlinked_claude(tmp_path)

    descriptor = resolve_claude({"PATH": str(command.parent)})

    assert descriptor.path == command.resolve()
    assert descriptor.version == "2.1.218 (Claude Code)"
    assert descriptor.sha256 == sha256_file(command.resolve())


def test_claude_revalidation_detects_hardlink_alias_mutation(tmp_path: Path) -> None:
    command, native = hardlinked_claude(tmp_path)
    environment = {"PATH": str(command.parent)}
    descriptor = resolve_claude(environment)
    native.write_text("#!/bin/sh\nprintf '%s\\n' '2.1.218 (Claude Code)'\n# changed\n")

    with pytest.raises(ProviderExecutableError) as caught:
        revalidate_claude(descriptor, environment)

    assert caught.value.code == "CONTROLLER_IMAGE_CHANGED"


def test_relative_override_fails_without_path_fallback(tmp_path: Path) -> None:
    path_codex = executable(tmp_path / "bin/codex", "codex-cli 0.154.0")

    with pytest.raises(ProviderExecutableError) as caught:
        resolve_codex(
            {
                "PATH": str(path_codex.parent),
                "AIZIM_CODEX_EXECUTABLE": "relative-codex",
            }
        )

    assert caught.value.code == "CODEX_EXECUTABLE_UNAVAILABLE"


@pytest.mark.parametrize(
    "version",
    ["0.145.0", "0.146.0", "0.154.0-alpha.3", "0.155.0"],
)
def test_unsupported_version_reports_observed_supported_path_and_override(
    tmp_path: Path,
    version: str,
) -> None:
    codex = executable(tmp_path / "codex", f"codex-cli {version}").resolve()

    with pytest.raises(ProviderExecutableError) as caught:
        resolve_codex({"AIZIM_CODEX_EXECUTABLE": str(codex), "PATH": ""})

    assert caught.value.code == "UNSUPPORTED_CODEX_VERSION"
    detail = str(caught.value)
    assert str(codex) in detail
    assert f"codex-cli {version}" in detail
    assert "codex-cli 0.154.0" in detail
    assert "AIZIM_CODEX_EXECUTABLE" in detail
    assert "side-by-side" in detail


def test_descriptor_revalidation_detects_image_replacement(tmp_path: Path) -> None:
    codex = executable(tmp_path / "codex", "codex-cli 0.154.0")
    environment = {"AIZIM_CODEX_EXECUTABLE": str(codex), "PATH": ""}
    descriptor = resolve_codex(environment)
    codex.write_text("#!/bin/sh\nprintf '%s\\n' 'codex-cli 0.154.0'\n# changed\n")

    with pytest.raises(ProviderExecutableError) as caught:
        revalidate_codex(descriptor, environment)

    assert caught.value.code == "CODEX_IMAGE_CHANGED"


def test_resolution_does_not_capture_environment_at_import_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = executable(tmp_path / "first/codex", "codex-cli 0.154.0")
    second = executable(tmp_path / "second/codex", "codex-cli 0.154.0")
    monkeypatch.setenv("AIZIM_CODEX_EXECUTABLE", str(first))
    first_descriptor = resolve_codex(dict(os.environ))
    monkeypatch.setenv("AIZIM_CODEX_EXECUTABLE", str(second))

    assert first_descriptor.path == first.resolve()
    assert resolve_codex(dict(os.environ)).path == second.resolve()


def test_external_codex_js_layout_locates_runtime_ripgrep_and_bwrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "lib/node_modules/@openai/codex"
    codex = executable(package / "bin/codex.js", "codex-cli 0.154.0").resolve()
    (package / "package.json").write_text('{"name":"@openai/codex"}\n')
    triple = package / "node_modules/@openai/codex-test" / "vendor/test-triple"
    ripgrep = executable(triple / "codex-path/rg", "ripgrep")
    bwrap = executable(triple / "codex-resources/bwrap", "bwrap")
    monkeypatch.setattr(
        providers,
        "_host_codex_layout",
        lambda: ("test", "test-triple"),
    )

    assert codex_runtime_root(codex) == package.resolve()
    assert codex_ripgrep_executable(codex) == ripgrep.resolve()
    assert codex_bwrap_executable(codex) == bwrap.resolve()
