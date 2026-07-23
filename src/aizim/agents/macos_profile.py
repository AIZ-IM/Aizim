from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

from .sandbox import SandboxLaunchSpec, SandboxRequest

_PROFILE_ID: Final = "aizim-worker"
_BASE_PATH: Final = "/usr/bin:/bin:/usr/sbin:/sbin"
_FIXED_LAUNCH_POLICY: Final = (
    "sandbox",
    "--permission-profile",
    _PROFILE_ID,
    "--sandbox-state-disable-network",
    "--log-denials",
    "-C",
)


def compile_macos_profile(
    codex_executable: Path,
    request: SandboxRequest,
    developer_root: Path,
) -> SandboxLaunchSpec:
    shell_env = MappingProxyType(
        {
            "PATH": _BASE_PATH,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": str(request.scratch_root),
        }
    )
    permission = _permission_override(request, developer_root)
    environment = _environment_override(shell_env)
    overrides = (
        f"default_permissions={_toml_string(_PROFILE_ID)}",
        'approval_policy="never"',
        permission,
        environment,
    )
    argv = (
        str(codex_executable),
        *(item for override in overrides for item in ("-c", override)),
        *_FIXED_LAUNCH_POLICY,
        str(request.view_root),
        *request.command,
    )
    return SandboxLaunchSpec(
        platform_id="darwin",
        argv=argv,
        cwd=request.view_root,
        parent_env=MappingProxyType(dict(request.parent_env)),
        shell_env=shell_env,
        view_root=request.view_root,
        scratch_root=request.scratch_root,
        profile_id=_PROFILE_ID,
        policy_hash=_policy_contract_hash(),
    )


def validate_macos_profile(
    spec: SandboxLaunchSpec,
    project_root: Path,
    developer_root: Path,
    runtime_read_roots: tuple[Path, ...] = (),
) -> None:
    argv = spec.argv
    expected_environment = {
        "PATH": _BASE_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(spec.scratch_root),
    }
    if (
        spec.profile_id != _PROFILE_ID
        or spec.cwd != spec.view_root
        or not project_root.is_absolute()
        or not developer_root.is_absolute()
        or dict(spec.shell_env) != expected_environment
        or len(argv) < 17
        or not Path(argv[0]).is_absolute()
        or argv[9:15] != _FIXED_LAUNCH_POLICY
        or argv[15] != str(spec.view_root)
        or not Path(argv[16]).is_absolute()
        or argv[1:9:2] != ("-c",) * 4
    ):
        raise ValueError("invalid macOS sandbox profile")
    overrides = argv[2:9:2]
    if (
        overrides[0] != f"default_permissions={_toml_string(_PROFILE_ID)}"
        or overrides[1] != 'approval_policy="never"'
        or overrides[3] != _environment_override(spec.shell_env)
        or spec.policy_hash != _policy_contract_hash()
        or not _permission_is_strict(
            overrides[2],
            spec,
            project_root,
            developer_root,
            runtime_read_roots,
        )
    ):
        raise ValueError("invalid macOS sandbox profile")


def _permission_is_strict(
    value: str,
    spec: SandboxLaunchSpec,
    project_root: Path,
    developer_root: Path,
    runtime_read_roots: tuple[Path, ...],
) -> bool:
    try:
        document = tomllib.loads(value)
        profile = document["permissions"][_PROFILE_ID]
        filesystem = profile["filesystem"]
        network = profile["network"]
    except (KeyError, TypeError, tomllib.TOMLDecodeError):
        return False
    if (
        type(profile) is not dict
        or profile.keys() != {"filesystem", "network"}
        or type(filesystem) is not dict
        or type(network) is not dict
        or network != {"enabled": False}
    ):
        return False
    project = str(project_root)
    expected_filesystem = {
        ":minimal": "read",
        str(developer_root): "read",
        **{str(root): "read" for root in runtime_read_roots},
        str(spec.view_root): "read",
        str(spec.scratch_root): "write",
        f"{project}/.aizim": "deny",
        f"{project}/.aizim/**": "deny",
        project: "deny",
        f"{project}/**": "deny",
    }
    return filesystem == expected_filesystem


def _permission_override(request: SandboxRequest, developer_root: Path) -> str:
    filesystem = (
        (":minimal", "read"),
        (str(developer_root), "read"),
        *((str(root), "read") for root in request.runtime_read_roots),
        (str(request.view_root), "read"),
        (str(request.scratch_root), "write"),
        (str(request.project_root / ".aizim"), "deny"),
        (str(request.project_root / ".aizim" / "**"), "deny"),
        (str(request.project_root), "deny"),
        (str(request.project_root / "**"), "deny"),
    )
    entries = ",".join(
        f"{_toml_string(path)}={_toml_string(permission)}" for path, permission in filesystem
    )
    return f"permissions.{_PROFILE_ID}={{filesystem={{{entries}}},network={{enabled=false}}}}"


def _policy_contract_hash() -> str:
    scratch = Path("/__aizim_contract__/scratch")
    request = SandboxRequest(
        Path("/__aizim_contract__/project"),
        Path("/__aizim_contract__/view"),
        scratch,
        (),
        {},
        (
            Path("/__aizim_contract__/runtime-python"),
            Path("/__aizim_contract__/runtime-codex"),
        ),
    )
    environment = {
        "PATH": _BASE_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(scratch),
    }
    contract = (
        f"default_permissions={_toml_string(_PROFILE_ID)}",
        'approval_policy="never"',
        _permission_override(request, Path("/__aizim_contract__/developer")),
        _environment_override(environment),
        *_FIXED_LAUNCH_POLICY,
    )
    body = json.dumps(contract, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def _environment_override(environment: Mapping[str, str]) -> str:
    values = ",".join(f"{key}={_toml_string(value)}" for key, value in environment.items())
    return (
        "shell_environment_policy="
        f'{{inherit="none",ignore_default_excludes=false,set={{{values}}}}}'
    )


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
