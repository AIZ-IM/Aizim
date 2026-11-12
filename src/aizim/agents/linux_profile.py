from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import MappingProxyType

from .permission_profile import sandbox_environment
from .sandbox import SandboxContractError, SandboxLaunchSpec, SandboxRequest

_PROFILE_ID = "aizim-worker"
_SYSTEM_READ_ROOTS = (
    "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/ld.so.cache", "/etc/ssl/certs",
    "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf",
)


def compile_linux_profile(
    sandbox_executable: Path,
    request: SandboxRequest,
) -> SandboxLaunchSpec:
    environment = sandbox_environment(request)
    # bwrap cannot filter destinations on a shared network namespace. The local magpie
    # gateway needs that namespace; this profile makes no TCP confinement claim.
    policy = [
        str(sandbox_executable), "--die-with-parent", "--new-session",
        "--unshare-all", "--share-net", "--cap-drop", "ALL",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
    ]
    for root in _SYSTEM_READ_ROOTS:
        if Path(root).exists():
            policy.extend(("--ro-bind", root, root))
    for root in request.runtime_read_roots:
        policy.extend(("--ro-bind", str(root), str(root)))
    policy.extend((
        "--ro-bind", str(request.view_root), str(request.view_root),
        "--bind", str(request.scratch_root), str(request.scratch_root),
        "--chdir", str(request.view_root), "--clearenv",
    ))
    for key, value in environment.items():
        policy.extend(("--setenv", key, value))
    policy.append("--")
    digest = hashlib.sha256(json.dumps({
        "profile": _PROFILE_ID,
        "filesystem": "private-root;runtime-and-view-readonly;scratch-writable",
        "network": "host-network-no-destination-confinement",
        "system_read_roots": _SYSTEM_READ_ROOTS,
        "namespaces": ("--unshare-all", "--share-net", "--cap-drop", "ALL"),
    }, separators=(",", ":")).encode()).hexdigest()
    return SandboxLaunchSpec(
        platform_id="linux", argv=(*policy, *request.command), cwd=request.view_root,
        parent_env=MappingProxyType(environment), shell_env=MappingProxyType(environment),
        view_root=request.view_root, scratch_root=request.scratch_root,
        profile_id=_PROFILE_ID, policy_hash=digest,
    )


def validate_linux_profile(
    spec: SandboxLaunchSpec, request: SandboxRequest, sandbox_executable: Path,
) -> None:
    if spec != compile_linux_profile(sandbox_executable, request):
        raise SandboxContractError("invalid Linux sandbox profile")
