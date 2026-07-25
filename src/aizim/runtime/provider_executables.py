from __future__ import annotations

import os
import platform
import shutil
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from aizim.domain import sha256_file

CODEX_VERSIONS: Final = frozenset({"codex-cli 0.145.0"})
CLAUDE_VERSIONS: Final = frozenset({"2.1.218 (Claude Code)"})
_VERSION_ENVIRONMENT: Final = frozenset({"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR"})


class ProviderExecutableError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class ResolvedExecutable:
    path: Path
    version: str
    sha256: str


def resolve_codex(environ: Mapping[str, str]) -> ResolvedExecutable:
    return _resolve(
        discover_codex_executable(environ),
        environ,
        CODEX_VERSIONS,
        "UNSUPPORTED_CODEX_VERSION",
        "AIZIM_CODEX_EXECUTABLE",
    )


def resolve_claude(environ: Mapping[str, str]) -> ResolvedExecutable:
    return _resolve(
        discover_claude_executable(environ),
        environ,
        CLAUDE_VERSIONS,
        "CONTROLLER_VERSION_UNSUPPORTED",
        "AIZIM_CLAUDE_EXECUTABLE",
    )


def discover_codex_executable(environ: Mapping[str, str]) -> Path:
    return _discover(
        environ,
        override_name="AIZIM_CODEX_EXECUTABLE",
        command="codex",
        unavailable_code="CODEX_EXECUTABLE_UNAVAILABLE",
    )


def discover_claude_executable(environ: Mapping[str, str]) -> Path:
    return _discover(
        environ,
        override_name="AIZIM_CLAUDE_EXECUTABLE",
        command="claude",
        unavailable_code="CLAUDE_EXECUTABLE_UNAVAILABLE",
    )


def revalidate_codex(
    executable: ResolvedExecutable,
    environ: Mapping[str, str],
) -> None:
    _revalidate(
        executable,
        environ,
        CODEX_VERSIONS,
        "UNSUPPORTED_CODEX_VERSION",
        "CODEX_IMAGE_CHANGED",
        "AIZIM_CODEX_EXECUTABLE",
    )


def revalidate_claude(
    executable: ResolvedExecutable,
    environ: Mapping[str, str],
) -> None:
    _revalidate(
        executable,
        environ,
        CLAUDE_VERSIONS,
        "CONTROLLER_VERSION_UNSUPPORTED",
        "CONTROLLER_IMAGE_CHANGED",
        "AIZIM_CLAUDE_EXECUTABLE",
    )


def codex_runtime_root(executable: Path) -> Path:
    package = executable.parent.parent
    if (
        executable.name == "codex.js"
        and executable.parent.name == "bin"
        and (package / "package.json").is_file()
    ):
        return package.resolve(strict=True)
    return next(
        (root for root in executable.parents if root.name == "vendor"),
        executable.parent,
    ).resolve(strict=True)


def codex_ripgrep_executable(executable: Path) -> Path:
    candidate = _codex_native_root(executable) / "codex-path" / "rg"
    return _canonical_executable(candidate, "RIPGREP_EXECUTABLE_UNAVAILABLE")


def codex_bwrap_executable(executable: Path) -> Path:
    candidate = _codex_native_root(executable) / "codex-resources" / "bwrap"
    return _canonical_executable(candidate, "CODEX_SANDBOX_EXECUTABLE_UNAVAILABLE")


def _resolve(
    path: Path,
    environ: Mapping[str, str],
    supported: frozenset[str],
    unsupported_code: str,
    override_name: str,
) -> ResolvedExecutable:
    version = _version(path, environ)
    if version not in supported:
        raise _unsupported(
            unsupported_code,
            path,
            version,
            supported,
            override_name,
        )
    try:
        digest = sha256_file(path)
    except OSError as error:
        raise ProviderExecutableError("PROVIDER_EXECUTABLE_CHANGED") from error
    return ResolvedExecutable(path, version, digest)


def _revalidate(
    executable: ResolvedExecutable,
    environ: Mapping[str, str],
    supported: frozenset[str],
    unsupported_code: str,
    changed_code: str,
    override_name: str,
) -> None:
    try:
        current = _canonical_executable(executable.path, changed_code)
        version = _version(current, environ)
    except ProviderExecutableError as error:
        if error.code == changed_code:
            raise
        raise ProviderExecutableError(changed_code) from error
    if version not in supported:
        raise _unsupported(
            unsupported_code,
            current,
            version,
            supported,
            override_name,
        )
    try:
        digest = sha256_file(current)
    except OSError as error:
        raise ProviderExecutableError(changed_code) from error
    if current != executable.path or version != executable.version or digest != executable.sha256:
        raise ProviderExecutableError(changed_code)


def _discover(
    environ: Mapping[str, str],
    *,
    override_name: str,
    command: str,
    unavailable_code: str,
) -> Path:
    override = environ.get(override_name)
    candidate = override if override else shutil.which(command, path=environ.get("PATH"))
    if candidate is None:
        raise ProviderExecutableError(unavailable_code)
    path = Path(candidate)
    if override and not path.is_absolute():
        raise ProviderExecutableError(unavailable_code)
    return _canonical_executable(path, unavailable_code)


def _canonical_executable(path: Path, code: str) -> Path:
    if not path.is_absolute():
        raise ProviderExecutableError(code)
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as error:
        raise ProviderExecutableError(code) from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not os.access(resolved, os.X_OK)
    ):
        raise ProviderExecutableError(code)
    return resolved


def _version(path: Path, environ: Mapping[str, str]) -> str:
    from aizim.agents.launcher import (
        AgentLaunchError,
        HostCommandSpec,
        run_host_command,
    )

    environment = {name: environ[name] for name in _VERSION_ENVIRONMENT if name in environ}
    try:
        outcome = run_host_command(
            HostCommandSpec(
                (str(path), "--version"),
                Path(tempfile.gettempdir()).resolve(),
                environment,
            )
        )
        if outcome.returncode != 0:
            raise ProviderExecutableError("PROVIDER_VERSION_UNAVAILABLE")
        return outcome.stdout.decode().strip()
    except (AgentLaunchError, UnicodeDecodeError) as error:
        raise ProviderExecutableError("PROVIDER_VERSION_UNAVAILABLE") from error


def _unsupported(
    code: str,
    path: Path,
    observed: str,
    supported: frozenset[str],
    override_name: str,
) -> ProviderExecutableError:
    versions = ", ".join(sorted(supported))
    detail = (
        f"path={path}; observed={observed!r}; supported={versions!r}; "
        f"install a supported side-by-side CLI and set {override_name} "
        "to its absolute path"
    )
    return ProviderExecutableError(code, detail)


def _codex_native_root(executable: Path) -> Path:
    runtime = codex_runtime_root(executable)
    if executable.name != "codex.js":
        return executable.parent.parent
    package, triple = _host_codex_layout()
    return runtime / "node_modules" / "@openai" / f"codex-{package}" / "vendor" / triple


def _host_codex_layout() -> tuple[str, str]:
    layout = {
        ("darwin", "arm64"): ("darwin-arm64", "aarch64-apple-darwin"),
        ("linux", "aarch64"): ("linux-arm64", "aarch64-unknown-linux-musl"),
        ("linux", "x86_64"): ("linux-x64", "x86_64-unknown-linux-musl"),
    }.get((sys.platform, platform.machine().lower()))
    if layout is None:
        raise ProviderExecutableError("CODEX_RUNTIME_UNAVAILABLE")
    return layout
