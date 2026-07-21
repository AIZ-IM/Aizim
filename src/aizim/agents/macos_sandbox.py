from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .macos_profile import compile_macos_profile
from .sandbox import (
    ProbeOperation,
    ProbeReport,
    ProbeRequest,
    SandboxLaunchSpec,
    SandboxRequest,
    append_probe_event,
    parse_probe_attempts,
    probe_document,
    protected_asset_digests,
)

_CODEX_VERSION: Final = "codex-cli 0.144.6"
_SANDBOX_EXECUTABLE: Final = Path("/usr/bin/sandbox-exec")
_OUTPUT_LIMIT: Final = 256 * 1024


class SandboxHostError(RuntimeError):
    pass


class SandboxProbeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MacOSSandboxDependencies:
    platform: str
    codex_executable: Path
    codex_version: Callable[[], str]
    sandbox_is_apple: Callable[[], bool]
    developer_root: Callable[[], Path]


class MacOSSandboxAdapter:
    def __init__(self, dependencies: MacOSSandboxDependencies | None = None) -> None:
        self._dependencies = _default_dependencies() if dependencies is None else dependencies

    def compile(self, request: SandboxRequest) -> SandboxLaunchSpec:
        developer_root = self._validate_host()
        project_root = _resolved_directory(request.project_root, "project root")
        if any(
            root == project_root or root in project_root.parents
            for root in (Path("/private/tmp"), Path("/private/var/tmp"))
        ):
            raise SandboxHostError("canonical project cannot use the sandbox temporary root")
        view_root = _ephemeral_root(request.view_root, "aizim-view-")
        scratch_root = _ephemeral_root(request.scratch_root, "aizim-scratch-")
        if view_root.parent != scratch_root.parent:
            raise SandboxHostError("sandbox roots must share one private parent")
        if any(
            project_root == root or project_root in root.parents or root in project_root.parents
            for root in (view_root, scratch_root)
        ):
            raise SandboxHostError("sandbox roots cannot be inside the canonical project")
        if not request.command or not Path(request.command[0]).is_absolute():
            raise SandboxHostError("sandbox command must use an absolute executable")
        normalized = SandboxRequest(
            project_root,
            view_root,
            scratch_root,
            request.command,
            request.parent_env,
        )
        return compile_macos_profile(
            self._dependencies.codex_executable, normalized, developer_root
        )

    async def launch_probe(self, request: ProbeRequest) -> ProbeReport:
        protected_before = protected_asset_digests(request)
        logical_before = request.event_sink.logical_digest()
        document = probe_document(request)
        command = ("/usr/bin/python3", "-I", "-B", "-", document)
        spec = self.compile(
            SandboxRequest(
                request.project_root,
                request.view_root,
                request.scratch_root,
                command,
                request.parent_env,
            )
        )
        stdout, stderr = await _execute(spec, request.timeout_seconds)
        secret = request.parent_env.get(request.secret_environment_name)
        if secret and (secret.encode() in stdout or secret.encode() in stderr):
            raise SandboxProbeError("PROBE_OUTPUT_CONTAINED_SECRET")
        try:
            attempts = parse_probe_attempts(stdout)
        except ValueError as error:
            raise SandboxProbeError("INVALID_PROBE_OUTPUT") from error
        for attempt in attempts:
            append_probe_event(request, attempt)
        logical_after = request.event_sink.logical_digest()
        protected_after = protected_asset_digests(request)
        expected = tuple(ProbeOperation)
        verdicts_match = all(
            attempt.verdict == ("denied" if index < 9 else "allowed")
            and attempt.reason_code == ("SANDBOX_ENFORCED" if index < 9 else "OPERATION_ALLOWED")
            for index, attempt in enumerate(attempts)
        )
        unexpected_allows = tuple(
            attempt.operation for attempt in attempts[:9] if attempt.verdict == "allowed"
        )
        passed = (
            tuple(attempt.operation for attempt in attempts) == expected
            and verdicts_match
            and protected_before == protected_after
            and logical_before == logical_after
        )
        return ProbeReport(
            passed=passed,
            codex_version=_CODEX_VERSION,
            sandbox_executable=str(_SANDBOX_EXECUTABLE),
            policy_hash=spec.policy_hash,
            attempts=attempts,
            unexpected_allows=unexpected_allows,
            logical_digest_before=logical_before,
            logical_digest_after=logical_after,
        )

    def _validate_host(self) -> Path:
        dependencies = self._dependencies
        if dependencies.platform != "darwin":
            raise SandboxHostError("macOS sandbox requires Darwin")
        try:
            version = dependencies.codex_version()
            signed = dependencies.sandbox_is_apple()
            developer_root = dependencies.developer_root()
        except (OSError, subprocess.SubprocessError) as error:
            raise SandboxHostError("sandbox host validation failed") from error
        if not dependencies.codex_executable.is_absolute() or version != _CODEX_VERSION:
            raise SandboxHostError("unsupported Codex CLI")
        if not signed:
            raise SandboxHostError("Apple sandbox executable is unavailable")
        if not developer_root.is_absolute():
            raise SandboxHostError("developer root must be absolute")
        return _resolved_directory(developer_root, "developer root")


def _default_dependencies() -> MacOSSandboxDependencies:
    executable = shutil.which("codex")
    codex = Path(executable) if executable is not None else Path("codex")
    return MacOSSandboxDependencies(
        platform=sys.platform,
        codex_executable=codex,
        codex_version=lambda: _command_output((str(codex), "--version")),
        sandbox_is_apple=_sandbox_is_apple,
        developer_root=lambda: Path(_command_output(("/usr/bin/xcode-select", "-p"))),
    )


def _command_output(argv: tuple[str, ...]) -> str:
    result = subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    return result.stdout.strip()


def _sandbox_is_apple() -> bool:
    if not _SANDBOX_EXECUTABLE.is_file():
        return False
    verified = subprocess.run(
        ("/usr/bin/codesign", "--verify", "--strict", str(_SANDBOX_EXECUTABLE)),
        check=False,
        capture_output=True,
        timeout=5.0,
    )
    requirement = subprocess.run(
        ("/usr/bin/codesign", "-d", "-r-", str(_SANDBOX_EXECUTABLE)),
        check=False,
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    details = requirement.stdout + requirement.stderr
    return (
        verified.returncode == 0
        and requirement.returncode == 0
        and 'identifier "com.apple.sandbox-exec"' in details
        and "anchor apple" in details
    )


def _resolved_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise SandboxHostError(f"{label} is unavailable") from error
    if not resolved.is_dir():
        raise SandboxHostError(f"{label} is not a directory")
    return resolved


def _ephemeral_root(path: Path, prefix: str) -> Path:
    resolved = _resolved_directory(path, "ephemeral root")
    parent_metadata = resolved.parent.stat()
    root_mode = resolved.stat().st_mode & 0o777
    private_parent = parent_metadata.st_uid == os.getuid() and parent_metadata.st_mode & 0o077 == 0
    if not resolved.name.startswith(prefix) or root_mode != 0o700 or not private_parent:
        raise SandboxHostError("sandbox roots must use a private temporary parent")
    return resolved


async def _execute(spec: SandboxLaunchSpec, timeout_seconds: float) -> tuple[bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *spec.argv,
        cwd=spec.cwd,
        env=dict(spec.parent_env),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    source = Path(__file__).with_name("attack_probe.py").read_bytes()
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(source), timeout=timeout_seconds
        )
    except BaseException as error:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.communicate()
        if isinstance(error, TimeoutError):
            raise SandboxProbeError("PROBE_TIMEOUT") from error
        raise
    if process.returncode != 0:
        raise SandboxProbeError("PROBE_PROCESS_FAILED")
    if len(stdout) > _OUTPUT_LIMIT or len(stderr) > _OUTPUT_LIMIT:
        raise SandboxProbeError("PROBE_OUTPUT_LIMIT")
    return stdout, stderr
