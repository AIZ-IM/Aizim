from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from aizim.agents.linux_profile import compile_linux_profile, validate_linux_profile
from aizim.agents.permission_profile import normalize_sandbox_request
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
        project.resolve(), view.resolve(), scratch.resolve(),
        ("/usr/bin/python3", "-I", "-B", "-"),
        {"PATH": "/host/path", "AIZIM_SECRET": "hidden"}, (runtime.resolve(),),
    )


def test_linux_profile_has_private_root_readonly_views_and_host_network(tmp_path: Path) -> None:
    first_request = normalize_sandbox_request(request(tmp_path, "one"))
    second_request = normalize_sandbox_request(request(tmp_path, "two"))
    bwrap = Path("/usr/bin/bwrap")
    first = compile_linux_profile(bwrap, first_request)
    second = compile_linux_profile(bwrap, second_request)

    assert first.platform_id == "linux"
    assert first.profile_id == "aizim-worker"
    assert first.policy_hash == second.policy_hash
    assert first.argv[0] == str(bwrap)
    assert "--unshare-all" in first.argv
    assert "--share-net" in first.argv
    assert first.argv[first.argv.index("--cap-drop"):][:2] == ("--cap-drop", "ALL")
    for root in (*first_request.runtime_read_roots, first_request.view_root):
        index = first.argv.index(str(root))
        assert first.argv[index - 1:index + 2] == ("--ro-bind", str(root), str(root))
    index = first.argv.index(str(first_request.scratch_root))
    assert first.argv[index - 1:index + 2] == (
        "--bind", str(first_request.scratch_root), str(first_request.scratch_root),
    )
    assert str(first_request.project_root) not in first.argv
    assert first.argv[-len(first_request.command) - 1:] == ("--", *first_request.command)
    assert first.cwd == first_request.view_root
    assert first.shell_env == first.parent_env == {
        "PATH": "/host/path", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "HOME": str(first_request.scratch_root), "TMPDIR": str(first_request.scratch_root),
    }
    assert "--clearenv" in first.argv
    assert "hidden" not in repr(first)
    assert all("hidden" not in value for value in first.argv)


@pytest.mark.parametrize("mutation", ("network", "view", "scratch", "command", "environment"))
def test_linux_validator_rejects_profile_tampering(tmp_path: Path, mutation: str) -> None:
    sandbox_request = normalize_sandbox_request(request(tmp_path, "tamper"))
    bwrap = Path("/usr/bin/bwrap")
    spec = compile_linux_profile(bwrap, sandbox_request)
    argv = list(spec.argv)
    if mutation == "network":
        argv.remove("--unshare-all")
    elif mutation == "view":
        argv[argv.index(str(sandbox_request.view_root)) - 1] = "--bind"
    elif mutation == "scratch":
        argv[argv.index(str(sandbox_request.scratch_root)) - 1] = "--ro-bind"
    elif mutation == "command":
        argv[-1] = "--different-command"
    else:
        argv[argv.index("--clearenv")] = "--keepenv"

    with pytest.raises(ValueError, match="invalid Linux sandbox profile"):
        validate_linux_profile(replace(spec, argv=tuple(argv)), sandbox_request, bwrap)


def test_linux_validator_rejects_runtime_root_tampering(tmp_path: Path) -> None:
    sandbox_request = normalize_sandbox_request(request(tmp_path, "root-tamper"))
    bwrap = Path("/usr/bin/bwrap")
    spec = compile_linux_profile(bwrap, sandbox_request)
    argv = tuple(
        value.replace(str(sandbox_request.runtime_read_roots[0]), str(tmp_path / "other-runtime"))
        for value in spec.argv
    )

    with pytest.raises(ValueError, match="invalid Linux sandbox profile"):
        validate_linux_profile(replace(spec, argv=argv), sandbox_request, bwrap)
