from __future__ import annotations

import os
import stat
from pathlib import Path

from .sandbox import ProviderEnvironmentPolicy, SandboxContractError, SandboxRequest

_PROVIDER_ENVIRONMENT_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "CODEX_HOME",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "TMPDIR",
    }
)
_BASELINE_ENVIRONMENT_KEYS = frozenset({"PATH", "LANG", "LC_ALL", "TMPDIR"})


def normalize_sandbox_request(
    request: SandboxRequest,
    *,
    forbidden_project_roots: tuple[Path, ...] = (),
) -> SandboxRequest:
    project = canonical_directory(request.project_root, "project root")
    if any(root == project or root in project.parents for root in forbidden_project_roots):
        raise SandboxContractError("canonical project cannot use a shared temporary root")
    view = private_ephemeral_root(request.view_root, "aizim-view-")
    scratch = private_ephemeral_root(request.scratch_root, "aizim-scratch-")
    if view.parent != scratch.parent:
        raise SandboxContractError("sandbox roots must share one private parent")
    if any(paths_overlap(project, root) for root in (view, scratch)):
        raise SandboxContractError("sandbox roots cannot overlap the canonical project")
    if not request.command or not Path(request.command[0]).is_absolute():
        raise SandboxContractError("sandbox command must use an absolute executable")
    if type(request.provider_environment) is not ProviderEnvironmentPolicy:
        raise SandboxContractError("invalid provider environment policy")
    if request.provider_environment is ProviderEnvironmentPolicy.FILTERED_PARENT:
        _validate_provider_environment(request)

    runtime_roots: list[Path] = []
    for root in request.runtime_read_roots:
        canonical = canonical_directory(
            root,
            "runtime read root",
            require_exact=True,
        )
        if any(paths_overlap(canonical, protected) for protected in (project, view, scratch)):
            raise SandboxContractError("runtime read root overlaps a protected root")
        if canonical not in runtime_roots:
            runtime_roots.append(canonical)
    return SandboxRequest(
        project,
        view,
        scratch,
        request.command,
        request.parent_env,
        tuple(runtime_roots),
        request.provider_environment,
    )


def _validate_provider_environment(request: SandboxRequest) -> None:
    environment = request.parent_env
    if (
        not environment.keys() >= _BASELINE_ENVIRONMENT_KEYS
        or not environment.keys() <= _PROVIDER_ENVIRONMENT_KEYS
        or environment.get("TMPDIR") != str(request.scratch_root)
        or any(type(value) is not str or not value for value in environment.values())
    ):
        raise SandboxContractError("invalid provider environment")


def canonical_directory(
    path: Path,
    label: str,
    *,
    require_exact: bool = False,
) -> Path:
    if not path.is_absolute():
        raise SandboxContractError(f"{label} must be absolute")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise SandboxContractError(f"{label} is unavailable") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or (require_exact and resolved != path)
    ):
        raise SandboxContractError(f"{label} is not canonical")
    return resolved


def private_ephemeral_root(path: Path, prefix: str) -> Path:
    resolved = canonical_directory(path, "ephemeral root")
    parent_metadata = resolved.parent.stat()
    root_mode = resolved.stat().st_mode & 0o777
    private_parent = parent_metadata.st_uid == os.getuid() and parent_metadata.st_mode & 0o077 == 0
    if not resolved.name.startswith(prefix) or root_mode != 0o700 or not private_parent:
        raise SandboxContractError("sandbox roots must use a private temporary parent")
    return resolved


def paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents
