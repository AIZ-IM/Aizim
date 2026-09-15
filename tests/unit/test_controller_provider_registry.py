from __future__ import annotations

import stat
from pathlib import Path
from typing import cast

import pytest

from aizim.domain import ControllerProviderId
from aizim.orchestration.control_plane import ControlPlaneError, configure_controller
from aizim.orchestration.controller_providers import (
    ControllerProviderRegistryError,
    controller_adapter,
    controller_provider_ids,
    parse_controller_provider,
    resolve_controller_runtime,
)
from aizim.state.control_operations import ControlOperationTarget


@pytest.mark.parametrize("value", ("codex", "claude", "future_provider-1"))
def test_controller_provider_id_accepts_well_formed_values(value: str) -> None:
    assert ControllerProviderId(value).value == value


@pytest.mark.parametrize(
    "value",
    (
        "",
        "Codex",
        "1codex",
        "contains.dot",
        "contains/slash",
        "a" * 65,
        1,
    ),
)
def test_controller_provider_id_rejects_malformed_values(value: object) -> None:
    with pytest.raises(ValueError, match=r"^CONTROLLER_PROVIDER_INVALID$"):
        ControllerProviderId(cast(str, value))


def test_control_plane_rejects_a_value_that_is_not_a_provider_id() -> None:
    with pytest.raises(ControlPlaneError, match=r"^CONTROLLER_PROVIDER_INVALID$"):
        configure_controller(
            cast(ControlOperationTarget, object()),
            cast(ControllerProviderId, "codex"),
            None,
        )


def provider_executable(path: Path, version: str) -> Path:
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path


def test_registry_exposes_only_sorted_builtin_provider_ids() -> None:
    assert controller_provider_ids() == ("claude", "codex")
    assert controller_adapter(ControllerProviderId("codex")).provider.value == "codex"
    assert controller_adapter(ControllerProviderId("claude")).provider.value == "claude"


@pytest.mark.parametrize("value", ("../codex", "Codex", ""))
def test_registry_distinguishes_malformed_provider_id(value: str) -> None:
    with pytest.raises(
        ControllerProviderRegistryError,
        match=r"^CONTROLLER_PROVIDER_INVALID$",
    ):
        parse_controller_provider(value)


def test_registry_distinguishes_well_formed_unsupported_provider() -> None:
    provider = parse_controller_provider("future_provider-1")

    with pytest.raises(
        ControllerProviderRegistryError,
        match=r"^CONTROLLER_PROVIDER_UNSUPPORTED$",
    ):
        controller_adapter(provider)


def test_codex_runtime_reuses_mandatory_worker_descriptor(tmp_path) -> None:
    codex = provider_executable(tmp_path / "codex", "codex-cli 0.154.0")

    runtime = resolve_controller_runtime(
        ControllerProviderId("codex"),
        {"AIZIM_CODEX_EXECUTABLE": str(codex), "PATH": ""},
    )

    assert runtime.provider == ControllerProviderId("codex")
    assert runtime.controller is runtime.codex


def test_claude_runtime_resolves_only_selected_controller(tmp_path) -> None:
    codex = provider_executable(tmp_path / "codex", "codex-cli 0.154.0")
    claude = provider_executable(tmp_path / "claude", "2.1.218 (Claude Code)")

    runtime = resolve_controller_runtime(
        ControllerProviderId("claude"),
        {
            "AIZIM_CODEX_EXECUTABLE": str(codex),
            "AIZIM_CLAUDE_EXECUTABLE": str(claude),
            "PATH": "",
        },
    )

    assert runtime.provider == ControllerProviderId("claude")
    assert runtime.codex.path == codex.resolve()
    assert runtime.controller.path == claude.resolve()
