from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from aizim.agents import macos_profile
from aizim.agents.macos_profile import validate_macos_profile
from aizim.agents.macos_sandbox import MacOSSandboxAdapter, MacOSSandboxDependencies
from aizim.agents.sandbox import SandboxRequest


def _dependencies() -> MacOSSandboxDependencies:
    return MacOSSandboxDependencies(
        platform="darwin",
        codex_executable=Path("/opt/homebrew/bin/codex"),
        codex_version=lambda: "codex-cli 0.145.0",
        sandbox_is_apple=lambda: True,
        developer_root=lambda: Path("/Library/Developer/CommandLineTools"),
    )


def _request(tmp_path: Path) -> SandboxRequest:
    project = tmp_path / "project"
    view = tmp_path / "aizim-view-contract"
    scratch = tmp_path / "aizim-scratch-contract"
    project.mkdir()
    view.mkdir(mode=0o700)
    scratch.mkdir(mode=0o700)
    return SandboxRequest(
        project.resolve(),
        view.resolve(),
        scratch.resolve(),
        ("/usr/bin/python3", "-I", "-B", "-"),
        {},
    )


@pytest.mark.parametrize(
    ("index", "replacement"),
    (
        pytest.param(10, "--other-profile-option", id="permission-profile-flag"),
        pytest.param(11, "other-profile", id="permission-profile-name"),
        pytest.param(12, None, id="network-disable"),
        pytest.param(13, None, id="denial-logging"),
        pytest.param(14, "--other-working-directory", id="working-directory-flag"),
        pytest.param(15, "other-view", id="working-directory-value"),
        pytest.param(16, "--extra-sandbox-option", id="command-boundary"),
    ),
)
def test_validator_rejects_fixed_launch_tail_tampering(
    tmp_path: Path, index: int, replacement: str | None
) -> None:
    sandbox_request = _request(tmp_path)
    spec = MacOSSandboxAdapter(_dependencies()).compile(sandbox_request)
    values = list(spec.argv)
    if replacement is None:
        del values[index]
    else:
        values[index] = replacement

    with pytest.raises(ValueError, match="invalid macOS sandbox profile"):
        validate_macos_profile(
            replace(spec, argv=tuple(values)),
            sandbox_request.project_root,
            _dependencies().developer_root(),
        )


def test_policy_hash_binds_fixed_launch_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    original = macos_profile._policy_contract_hash()
    monkeypatch.setattr(
        macos_profile,
        "_FIXED_LAUNCH_POLICY",
        ("sandbox", "--permission-profile", "aizim-worker", "--log-denials", "-C"),
    )

    assert macos_profile._policy_contract_hash() != original
