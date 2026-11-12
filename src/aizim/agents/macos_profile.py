from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import MappingProxyType

from .permission_profile import sandbox_environment
from .sandbox import SandboxContractError, SandboxLaunchSpec, SandboxRequest

_PROFILE_ID = "aizim-worker"
_SANDBOX_EXECUTABLE = Path("/usr/bin/sandbox-exec")


def _literal(path: Path) -> str:
    return json.dumps(str(path), ensure_ascii=False)


def _profile(request: SandboxRequest, developer_root: Path) -> str:
    # Deny by default; TCP is available to reach magpie, with no destination filtering.
    read_roots = (Path("/System"), Path("/usr"), Path("/bin"), Path("/sbin"),
                  Path("/Library/Apple"), Path("/private/var/db/dyld"),
                  developer_root, *request.runtime_read_roots, request.view_root)
    reads = "\n".join(
        f"(allow file-read* ({'literal' if root.is_file() else 'subpath'} {_literal(root)}))"
        for root in read_roots
    )
    return "\n".join((
        "(version 1)", "(deny default)",
        "(allow process-exec)", "(allow process-fork)", "(allow signal (target self))",
        "(allow sysctl-read)", "(allow mach-lookup)",
        '(allow file-read* file-write* (literal "/dev/null") (literal "/dev/zero") '
        '(literal "/dev/random") (literal "/dev/urandom"))',
        reads,
        f"(allow file-read* file-write* (subpath {_literal(request.scratch_root)}))",
        f"(deny file-read* file-write* (subpath {_literal(request.project_root)}))",
        '(allow network-outbound (remote tcp "*:*"))',
    ))


def compile_macos_profile(
    agent_executable: Path, request: SandboxRequest, developer_root: Path,
) -> SandboxLaunchSpec:
    del agent_executable
    environment = sandbox_environment(request)
    profile = _profile(request, developer_root)
    digest = hashlib.sha256(
        b"aizim-worker:seatbelt:readonly-runtime-view:writable-scratch:host-tcp"
    ).hexdigest()
    return SandboxLaunchSpec(
        platform_id="darwin", argv=(str(_SANDBOX_EXECUTABLE), "-p", profile, *request.command),
        cwd=request.view_root, parent_env=MappingProxyType(environment),
        shell_env=MappingProxyType(environment), view_root=request.view_root,
        scratch_root=request.scratch_root, profile_id=_PROFILE_ID, policy_hash=digest,
    )


def validate_macos_profile(
    spec: SandboxLaunchSpec, project_root: Path, developer_root: Path,
    runtime_read_roots: tuple[Path, ...] = (), *, provider_request: SandboxRequest | None = None,
) -> None:
    if provider_request is None or provider_request.project_root != project_root:
        raise SandboxContractError("missing macOS sandbox request")
    if provider_request.runtime_read_roots != runtime_read_roots:
        raise SandboxContractError("invalid macOS runtime roots")
    if spec != compile_macos_profile(
        Path(provider_request.command[0]), provider_request, developer_root,
    ):
        raise SandboxContractError("invalid macOS sandbox profile")
