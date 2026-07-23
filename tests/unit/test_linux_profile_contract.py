from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from aizim.agents.linux_profile import (
    compile_linux_profile,
    validate_linux_profile,
)
from aizim.agents.sandbox import SandboxRequest


def request(tmp_path: Path, name: str) -> SandboxRequest:
    parent = tmp_path / name
    parent.mkdir(mode=0o700)
    project = parent / "project"
    view = parent / "aizim-view-linux"
    scratch = parent / "aizim-scratch-linux"
    runtime = parent / "runtime"
    for path in (project, view, scratch, runtime):
        path.mkdir(mode=0o700)
    return SandboxRequest(
        project.resolve(),
        view.resolve(),
        scratch.resolve(),
        ("/usr/bin/python3", "-I", "-B", "-"),
        {"PATH": "/host/path", "AIZIM_SECRET": "hidden"},
        (runtime.resolve(),),
    )


def test_linux_profile_is_exact_closed_and_root_independent(tmp_path: Path) -> None:
    first_request = request(tmp_path, "one")
    second_request = request(tmp_path, "two")

    first = compile_linux_profile(Path("/opt/aizim/bin/codex"), first_request)
    second = compile_linux_profile(Path("/opt/aizim/bin/codex"), second_request)

    assert first.platform_id == "linux"
    assert first.profile_id == "aizim-worker"
    assert first.policy_hash == second.policy_hash
    assert first.shell_env == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(first_request.scratch_root),
    }
    assert first.argv[-len(first_request.command) :] == first_request.command
    assert 'approval_policy="never"' in first.argv
    assert "--log-denials" not in first.argv
    permission = next(
        value for value in first.argv if value.startswith("permissions.aizim-worker=")
    )
    assert "network={enabled=false}" in permission
    assert f'"{first_request.runtime_read_roots[0]}"="read"' in permission
    assert f'"{first_request.view_root}"="read"' in permission
    assert f'"{first_request.scratch_root}"="write"' in permission
    assert f'"{first_request.project_root}"="deny"' in permission
    assert str(first_request.project_root / ".aizim") not in permission
    assert f"{first_request.project_root}/**" not in permission
    assert "hidden" not in repr(first)


@pytest.mark.parametrize(
    ("index", "value"),
    [
        (0, "/other/codex"),
        (4, 'approval_policy="on-request"'),
        (6, "permissions.aizim-worker={network={enabled=true}}"),
        (10, "--dangerously-bypass-approvals-and-sandbox"),
        (12, "--allow-network"),
    ],
)
def test_linux_validator_reconstructs_the_full_profile(
    tmp_path: Path,
    index: int,
    value: str,
) -> None:
    sandbox_request = request(tmp_path, "tamper")
    spec = compile_linux_profile(Path("/opt/aizim/bin/codex"), sandbox_request)
    argv = list(spec.argv)
    argv[index] = value

    with pytest.raises(ValueError, match="invalid Linux sandbox profile"):
        validate_linux_profile(
            replace(spec, argv=tuple(argv)),
            sandbox_request,
            Path("/opt/aizim/bin/codex"),
        )


def test_linux_validator_rejects_concrete_runtime_root_tampering(
    tmp_path: Path,
) -> None:
    sandbox_request = request(tmp_path, "root-tamper")
    spec = compile_linux_profile(Path("/opt/aizim/bin/codex"), sandbox_request)
    argv = tuple(
        value.replace(
            str(sandbox_request.runtime_read_roots[0]),
            str(tmp_path / "other-runtime"),
        )
        for value in spec.argv
    )

    with pytest.raises(ValueError, match="invalid Linux sandbox profile"):
        validate_linux_profile(
            replace(spec, argv=argv),
            sandbox_request,
            Path("/opt/aizim/bin/codex"),
        )
