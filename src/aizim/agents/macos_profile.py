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
    policy_document = json.dumps(overrides, ensure_ascii=False, separators=(",", ":")).encode()
    argv = (
        str(codex_executable),
        *(item for override in overrides for item in ("-c", override)),
        "sandbox",
        "--permission-profile",
        _PROFILE_ID,
        "--sandbox-state-disable-network",
        "--log-denials",
        "-C",
        str(request.view_root),
        *request.command,
    )
    return SandboxLaunchSpec(
        argv=argv,
        cwd=request.view_root,
        parent_env=MappingProxyType(dict(request.parent_env)),
        shell_env=shell_env,
        view_root=request.view_root,
        scratch_root=request.scratch_root,
        profile_id=_PROFILE_ID,
        policy_hash=hashlib.sha256(policy_document).hexdigest(),
    )


def validate_macos_profile(spec: SandboxLaunchSpec) -> None:
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
        or dict(spec.shell_env) != expected_environment
        or len(argv) < 10
        or argv[9] != "sandbox"
        or argv[1:9:2] != ("-c",) * 4
    ):
        raise ValueError("invalid macOS sandbox profile")
    overrides = argv[2:9:2]
    expected_hash = hashlib.sha256(
        json.dumps(overrides, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        overrides[0] != f"default_permissions={_toml_string(_PROFILE_ID)}"
        or overrides[1] != 'approval_policy="never"'
        or overrides[3] != _environment_override(spec.shell_env)
        or spec.policy_hash != expected_hash
        or not _permission_is_strict(overrides[2], spec)
    ):
        raise ValueError("invalid macOS sandbox profile")


def _permission_is_strict(value: str, spec: SandboxLaunchSpec) -> bool:
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
    view, scratch = str(spec.view_root), str(spec.scratch_root)
    if filesystem.get(":minimal") != "read" or filesystem.get(view) != "read":
        return False
    if filesystem.get(scratch) != "write":
        return False
    writes = {path for path, access in filesystem.items() if access == "write"}
    denied = {path for path, access in filesystem.items() if access == "deny"}
    known_access = all(access in {"read", "write", "deny"} for access in filesystem.values())
    if writes != {scratch} or not known_access:
        return False
    roots = {
        path
        for path in denied
        if f"{path}/**" in denied and f"{path}/.aizim" in denied and f"{path}/.aizim/**" in denied
    }
    if len(roots) != 1:
        return False
    project = roots.pop()
    expected_denials = {project, f"{project}/**", f"{project}/.aizim", f"{project}/.aizim/**"}
    reads = {path for path, access in filesystem.items() if access == "read"}
    return denied == expected_denials and len(reads - {":minimal", view}) == 1


def _permission_override(request: SandboxRequest, developer_root: Path) -> str:
    filesystem = (
        (":minimal", "read"),
        (str(developer_root), "read"),
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


def _environment_override(environment: Mapping[str, str]) -> str:
    values = ",".join(f"{key}={_toml_string(value)}" for key, value in environment.items())
    return (
        "shell_environment_policy="
        f'{{inherit="none",ignore_default_excludes=false,set={{{values}}}}}'
    )


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
