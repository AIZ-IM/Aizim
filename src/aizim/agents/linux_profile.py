from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

from .sandbox import SandboxLaunchSpec, SandboxRequest

_PROFILE_ID: Final = "aizim-worker"
_BASE_PATH: Final = "/usr/local/bin:/usr/bin:/bin"
_FIXED_LAUNCH_POLICY: Final = (
    "sandbox",
    "--permission-profile",
    _PROFILE_ID,
    "--sandbox-state-disable-network",
    "-C",
)


def compile_linux_profile(
    codex_executable: Path,
    request: SandboxRequest,
) -> SandboxLaunchSpec:
    shell_env = MappingProxyType(
        {
            "PATH": _BASE_PATH,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": str(request.scratch_root),
        }
    )
    overrides = (
        f"default_permissions={_toml_string(_PROFILE_ID)}",
        'approval_policy="never"',
        _permission_override(request),
        _environment_override(shell_env),
    )
    return SandboxLaunchSpec(
        platform_id="linux",
        argv=(
            str(codex_executable),
            *(item for override in overrides for item in ("-c", override)),
            *_FIXED_LAUNCH_POLICY,
            str(request.view_root),
            *request.command,
        ),
        cwd=request.view_root,
        parent_env=MappingProxyType(dict(request.parent_env)),
        shell_env=shell_env,
        view_root=request.view_root,
        scratch_root=request.scratch_root,
        profile_id=_PROFILE_ID,
        policy_hash=_policy_contract_hash(),
    )


def validate_linux_profile(
    spec: SandboxLaunchSpec,
    request: SandboxRequest,
    codex_executable: Path,
) -> None:
    expected = compile_linux_profile(codex_executable, request)
    if spec != expected:
        raise ValueError("invalid Linux sandbox profile")


def _permission_override(request: SandboxRequest) -> str:
    filesystem = (
        (":minimal", "read"),
        *((str(root), "read") for root in request.runtime_read_roots),
        (str(request.view_root), "read"),
        (str(request.scratch_root), "write"),
        (str(request.project_root), "deny"),
    )
    entries = ",".join(
        f"{_toml_string(path)}={_toml_string(permission)}"
        for path, permission in filesystem
    )
    return (
        f"permissions.{_PROFILE_ID}="
        f"{{filesystem={{{entries}}},network={{enabled=false}}}}"
    )


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
        _permission_override(request),
        _environment_override(environment),
        *_FIXED_LAUNCH_POLICY,
    )
    body = json.dumps(contract, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def _environment_override(environment: Mapping[str, str]) -> str:
    values = ",".join(
        f"{key}={_toml_string(value)}" for key, value in environment.items()
    )
    return (
        "shell_environment_policy="
        f'{{inherit="none",ignore_default_excludes=false,set={{{values}}}}}'
    )


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
