from __future__ import annotations

import hashlib
import json
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
