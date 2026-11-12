from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from aizim.agents.macos_profile import compile_macos_profile, validate_macos_profile
from aizim.agents.permission_profile import normalize_sandbox_request
from aizim.agents.sandbox import SandboxRequest


def request(tmp_path: Path) -> tuple[SandboxRequest, Path]:
    tmp_path.chmod(0o700)
    project = tmp_path / 'project "quoted"'
    view = tmp_path / "aizim-view-contract"
    scratch = tmp_path / "aizim-scratch-contract"
    runtime = tmp_path / "runtime"
    developer = tmp_path / "developer"
    for path in (project, view, scratch, runtime, developer):
        path.mkdir(mode=0o700)
    return normalize_sandbox_request(SandboxRequest(
        project.resolve(), view.resolve(), scratch.resolve(),
        ("/usr/bin/python3", "-I", "-B", "-"),
        {"AIZIM_SECRET": "hidden"}, (runtime.resolve(),),
    )), developer.resolve()


def test_seatbelt_profile_denies_project_allows_host_tcp_and_scopes_writes(tmp_path: Path) -> None:
    sandbox_request, developer = request(tmp_path)
    spec = compile_macos_profile(Path("/usr/local/bin/omp"), sandbox_request, developer)
    profile = spec.argv[2]

    assert spec.argv[:2] == ("/usr/bin/sandbox-exec", "-p")
    assert spec.argv[3:] == sandbox_request.command
    assert "(deny default)" in profile
    assert '(allow network-outbound (remote tcp "*:*"))' in profile
    assert f'(allow file-read* (subpath "{sandbox_request.view_root}"))' in profile
    assert f'(allow file-read* (subpath "{sandbox_request.runtime_read_roots[0]}"))' in profile
    assert f'(allow file-read* file-write* (subpath "{sandbox_request.scratch_root}"))' in profile
    assert 'project \\"quoted\\"' in profile
    assert '(deny file-read* file-write* (subpath ' in profile
    assert "hidden" not in repr(spec)
    assert "AIZIM_SECRET" not in spec.parent_env
    validate_macos_profile(spec, sandbox_request.project_root, developer,
                           sandbox_request.runtime_read_roots, provider_request=sandbox_request)


@pytest.mark.parametrize("mutation", ("executable", "profile", "command", "cwd"))
def test_validator_rejects_launch_tampering(tmp_path: Path, mutation: str) -> None:
    sandbox_request, developer = request(tmp_path)
    spec = compile_macos_profile(Path("/usr/local/bin/omp"), sandbox_request, developer)
    argv = list(spec.argv)
    if mutation == "executable":
        argv[0] = "/other/sandbox-exec"
    elif mutation == "profile":
        argv[2] = argv[2].replace("(deny default)", "(allow default)")
    elif mutation == "command":
        argv[-1] = "other"
    else:
        spec = replace(spec, cwd=tmp_path)

    with pytest.raises(ValueError, match="invalid macOS sandbox profile"):
        validate_macos_profile(replace(spec, argv=tuple(argv)), sandbox_request.project_root,
                               developer, sandbox_request.runtime_read_roots,
                               provider_request=sandbox_request)


def test_validator_requires_matching_request_and_runtime_roots(tmp_path: Path) -> None:
    sandbox_request, developer = request(tmp_path)
    spec = compile_macos_profile(Path("/usr/local/bin/omp"), sandbox_request, developer)
    with pytest.raises(ValueError, match="missing macOS sandbox request"):
        validate_macos_profile(spec, sandbox_request.project_root, developer)
    with pytest.raises(ValueError, match="invalid macOS runtime roots"):
        validate_macos_profile(spec, sandbox_request.project_root, developer,
                               provider_request=sandbox_request)
