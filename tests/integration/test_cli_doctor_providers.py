from __future__ import annotations

from pathlib import Path

import pytest
from integration.doctor_provider_support import (
    PROVIDER_CHECK_IDS,
    descriptor,
    executable,
    initialized_project,
    persist_controller,
    provider_checks,
)

import aizim.cli.doctor_provider_checks as doctor_provider_checks
from aizim.cli import doctor_command
from aizim.cli.state_client import ProjectionDocument
from aizim.runtime.provider_executables import ProviderExecutableError


def test_codex_controller_never_resolves_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = initialized_project(tmp_path)
    persist_controller(root, "codex")
    resolved = descriptor(
        executable(tmp_path / "codex", "codex-cli 0.145.0"),
        "codex-cli 0.145.0",
    )

    checks = provider_checks(root, monkeypatch, codex=resolved)

    assert tuple(check.id for check in checks) == PROVIDER_CHECK_IDS
    assert tuple(check.status for check in checks) == ("PASS",) * 6
    expected = f"path={resolved.path} version={resolved.version} sha256={resolved.sha256}"
    assert checks[2].detail == expected
    assert checks[4].detail == expected


def test_claude_controller_uses_independent_codex_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = initialized_project(tmp_path)
    persist_controller(root, "claude")
    codex = descriptor(
        executable(tmp_path / "codex", "codex-cli 0.145.0"),
        "codex-cli 0.145.0",
    )
    claude = descriptor(
        executable(tmp_path / "claude", "2.1.218 (Claude Code)"),
        "2.1.218 (Claude Code)",
    )

    checks = provider_checks(root, monkeypatch, codex=codex, claude=claude)

    assert tuple(check.status for check in checks) == ("PASS",) * 6
    assert checks[2].detail == (
        f"path={claude.path} version={claude.version} sha256={claude.sha256}"
    )
    assert checks[4].detail == (f"path={codex.path} version={codex.version} sha256={codex.sha256}")


def test_selected_claude_failure_skips_only_controller_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = initialized_project(tmp_path)
    persist_controller(root, "claude")
    codex = descriptor(
        executable(tmp_path / "codex", "codex-cli 0.145.0"),
        "codex-cli 0.145.0",
    )

    checks = provider_checks(
        root,
        monkeypatch,
        codex=codex,
        claude=ProviderExecutableError("CLAUDE_EXECUTABLE_UNAVAILABLE"),
    )

    assert tuple(check.status for check in checks) == (
        "PASS",
        "PASS",
        "FAIL",
        "SKIP",
        "PASS",
        "PASS",
    )


def test_codex_controller_reuses_worker_version_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = initialized_project(tmp_path)
    persist_controller(root, "codex")

    checks = provider_checks(
        root,
        monkeypatch,
        codex=ProviderExecutableError("UNSUPPORTED_CODEX_VERSION"),
    )

    assert checks[2] == doctor_command.DoctorCheck(
        "controller_executable",
        "FAIL",
        "UNSUPPORTED_CODEX_VERSION",
    )
    assert checks[3].status == "SKIP"
    assert checks[4].detail == "UNSUPPORTED_CODEX_VERSION"


def test_missing_codex_keeps_claude_executable_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = initialized_project(tmp_path)
    persist_controller(root, "claude")
    claude = descriptor(
        executable(tmp_path / "claude", "2.1.218 (Claude Code)"),
        "2.1.218 (Claude Code)",
    )

    checks = provider_checks(
        root,
        monkeypatch,
        codex=ProviderExecutableError("CODEX_EXECUTABLE_UNAVAILABLE"),
        claude=claude,
    )

    assert tuple(check.status for check in checks) == (
        "PASS",
        "PASS",
        "PASS",
        "SKIP",
        "FAIL",
        "SKIP",
    )


@pytest.mark.parametrize(
    ("provider", "detail"),
    (
        ("../codex", "CONTROLLER_PROVIDER_INVALID"),
        ("future_provider-1", "CONTROLLER_PROVIDER_UNSUPPORTED"),
    ),
)
def test_malformed_and_unsupported_persisted_ids_fail_provider_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    detail: str,
) -> None:
    root = initialized_project(tmp_path)
    record = ProjectionDocument(
        "primary",
        "controller",
        {"payload": {"controller_id": "primary", "provider": provider}},
        1,
    )
    monkeypatch.setattr(doctor_provider_checks, "load_projections", lambda _layout: (record,))
    codex = descriptor(
        executable(tmp_path / "codex", "codex-cli 0.145.0"),
        "codex-cli 0.145.0",
    )

    checks = provider_checks(root, monkeypatch, codex=codex)

    assert checks[0].status == "PASS"
    assert checks[1] == doctor_command.DoctorCheck(
        "controller_provider",
        "FAIL",
        detail,
    )
    assert tuple(check.status for check in checks[2:4]) == ("SKIP", "SKIP")
    assert tuple(check.status for check in checks[4:]) == ("PASS", "PASS")
