from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

from .permission_profile import network_launch_policy, network_policy
from .sandbox import SandboxContractError, SandboxLaunchSpec, SandboxRequest

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
    filtered = request.provider_environment.value == "filtered_parent"
    shell_env = _shell_environment(request)
    permission = _permission_override(request, developer_root)
    environment = _environment_override(filtered, shell_env)
    overrides = (
        f"default_permissions={_toml_string(_PROFILE_ID)}",
        'approval_policy="never"',
        permission,
        environment,
    )
    argv = (
        str(codex_executable),
        *(item for override in overrides for item in ("-c", override)),
        *network_launch_policy(_FIXED_LAUNCH_POLICY, request.provider_network_domains),
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
        policy_hash=_policy_contract_hash(filtered, request.provider_network_domains),
    )


def validate_macos_profile(
    spec: SandboxLaunchSpec,
    project_root: Path,
    developer_root: Path,
    runtime_read_roots: tuple[Path, ...] = (),
    *,
    provider_request: SandboxRequest | None = None,
) -> None:
    if provider_request is not None and provider_request.provider_network_domains:
        if spec != compile_macos_profile(Path(spec.argv[0]), provider_request, developer_root):
            raise SandboxContractError("invalid macOS provider transport profile")
        return
    argv = spec.argv
    overrides = argv[2:9:2]
    filtered = len(overrides) == 4 and overrides[3] == _environment_override(True, {})
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
        raise SandboxContractError("invalid macOS sandbox profile")
    if (
        overrides[0] != f"default_permissions={_toml_string(_PROFILE_ID)}"
        or overrides[1] != 'approval_policy="never"'
        or overrides[3] != _environment_override(filtered, spec.shell_env)
        or spec.policy_hash != _policy_contract_hash(filtered)
        or not _permission_is_strict(
            overrides[2],
            spec,
            project_root,
            developer_root,
            runtime_read_roots,
        )
    ):
        raise SandboxContractError("invalid macOS sandbox profile")


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
    network = network_policy(request.provider_network_domains)
    return f"permissions.{_PROFILE_ID}={{filesystem={{{entries}}},network={network}}}"


def _policy_contract_hash(filtered: bool = False, domains: tuple[str, ...] = ()) -> str:
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
        provider_network_domains=domains,
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
        _environment_override(filtered, environment),
        *network_launch_policy(_FIXED_LAUNCH_POLICY, domains),
    )
    body = json.dumps(contract, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def _shell_environment(request: SandboxRequest) -> Mapping[str, str]:
    return MappingProxyType(
        {
            "PATH": _BASE_PATH,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": str(request.scratch_root),
        }
    )


def _environment_override(filtered: bool, environment: Mapping[str, str]) -> str:
    if filtered:
        return 'shell_environment_policy={inherit="all",ignore_default_excludes=true}'
    values = ",".join(f"{key}={_toml_string(value)}" for key, value in environment.items())
    return (
        "shell_environment_policy="
        f'{{inherit="none",ignore_default_excludes=false,set={{{values}}}}}'
    )


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
