from __future__ import annotations

import json
import os
import shutil
import ssl
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Never

from aizim.agents import BackendIdentity
from aizim.agents.macos_sandbox import SandboxHostError
from aizim.agents.platform_sandbox import sandbox_adapter
from aizim.agents.sandbox import ProviderEnvironmentPolicy, SandboxRequest
from aizim.domain import canonical_json, sha256_file
from aizim.domain.serialization import JsonValue
from aizim.runtime.provider_executables import (
    ProviderExecutableError,
    ResolvedExecutable,
    revalidate_claude,
    revalidate_codex,
)

from . import controller_backend as cb
from . import controller_process as process
from .controller_transport import CLAUDE_DOMAINS, prepare_auth

_SCHEMA_PATH: Final = Path(__file__).with_name("controller_wire.schema.json")
_SCHEMA: Final = canonical_json(json.loads(_SCHEMA_PATH.read_text())).decode()
_AUTH_ENVIRONMENT: Final = frozenset(
    {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "SSL_CERT_FILE", "SSL_CERT_DIR"}
)
_COMMAND_FLAGS: Final = (
    "-p",
    "--output-format",
    "json",
    "--json-schema",
    _SCHEMA,
    "--safe-mode",
    "--disable-slash-commands",
    "--setting-sources",
    "",
    "--permission-mode",
    "plan",
    "--no-chrome",
    "--tools",
    "",
    "--disallowedTools",
    "mcp__*",
    "--strict-mcp-config",
    "--no-session-persistence",
)


class ClaudeControllerError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ClaudeControllerBackend:
    def __init__(
        self,
        executable: ResolvedExecutable,
        sandbox_codex: ResolvedExecutable,
        model: str | None,
        project_root: Path,
        parent_environment: Mapping[str, str],
        launch: process.ControllerLauncher = process.launch_controller_process,
    ) -> None:
        try:
            revalidate_claude(executable, parent_environment)
        except ProviderExecutableError as error:
            _raise_provider_error(error)
        try:
            revalidate_codex(sandbox_codex, parent_environment)
        except ProviderExecutableError as error:
            raise ClaudeControllerError("CONTROLLER_SANDBOX_UNAVAILABLE") from error
        self._descriptor = executable
        self._executable = executable.path
        self._project = _canonical_project(project_root)
        if model is not None and (type(model) is not str or not model):
            raise ClaudeControllerError("CONTROLLER_MODEL_INVALID")
        self._model, self._source_environment, self._launch = (
            model,
            dict(parent_environment),
            launch,
        )
        self._sandbox_descriptor = sandbox_codex
        self._sandbox_executable = sandbox_codex.path
        self._image_hash = executable.sha256
        self._identity = BackendIdentity("claude", executable.version, executable.sha256)

    @property
    def identity(self) -> BackendIdentity:
        return self._identity

    async def preflight(self) -> None:
        self._validate_image()
        with process.private_workspace("aizim-controller-") as roots:
            private, _view, _scratch = roots
            try:
                command = (str(self._executable), "auth", "status", "--json")
                outcome = await self._run(private, command, b"", 10.0)
            except process.ControllerLaunchError as error:
                raise ClaudeControllerError("CONTROLLER_AUTH_FAILED") from error
            if outcome.exit_code != 0:
                raise ClaudeControllerError("CONTROLLER_AUTH_FAILED")

    async def plan(self, context: cb.ControllerContext) -> cb.ControllerDecision:
        try:
            return await self._plan(context)
        except ClaudeControllerError as error:
            raise cb.ControllerBackendError("CONTROLLER_DECISION_INVALID") from error

    async def _plan(self, context: cb.ControllerContext) -> cb.ControllerDecision:
        self._validate_image()
        with process.private_workspace("aizim-controller-") as roots:
            private, _view, _scratch = roots
            model = () if self._model is None else ("--model", self._model)
            command = (str(self._executable), *_COMMAND_FLAGS, *model)
            try:
                outcome = await self._run(
                    private,
                    command,
                    cb.controller_context_bytes(context),
                    context.max_timeout_seconds,
                    network=True,
                )
            except process.ControllerLaunchError as error:
                if error.code == "CONTROLLER_TIMEOUT":
                    raise TimeoutError from error
                raise ClaudeControllerError("CONTROLLER_PROCESS_FAILED") from error
            if outcome.exit_code != 0:
                raise ClaudeControllerError("CONTROLLER_PROCESS_FAILED")
            return _read_decision(outcome.stdout, context)

    async def _run(
        self,
        private: Path,
        command: tuple[str, ...],
        source: bytes,
        timeout_seconds: float,
        *,
        network: bool = False,
    ) -> process.ControllerLaunchOutcome:
        view, scratch = private / "aizim-view-empty", private / "aizim-scratch-data"
        runtime_executable = self._executable
        if self._project in runtime_executable.parents:
            runtime_executable = private / "home" / runtime_executable.name
            shutil.copyfile(self._executable, runtime_executable)
            runtime_executable.chmod(0o500)
            if sha256_file(runtime_executable) != self._image_hash:
                raise ClaudeControllerError("CONTROLLER_IMAGE_CHANGED")
            command = (str(runtime_executable), *command[1:])
        environment = _environment(self._source_environment, private, scratch)
        auth_key, auth_root = prepare_auth("claude", self._source_environment, scratch)
        environment[auth_key] = str(auth_root)
        environment["TMPDIR"] = str(scratch / "tmp")
        if network:
            command = ("/usr/bin/env", f"TMPDIR={scratch / 'client-tmp'}", *command)
        request = SandboxRequest(
            self._project,
            view,
            scratch,
            command,
            environment,
            _runtime_roots(runtime_executable),
            ProviderEnvironmentPolicy.FILTERED_PARENT,
            CLAUDE_DOMAINS if network else (),
        )
        try:
            revalidate_codex(self._sandbox_descriptor, self._source_environment)
        except ProviderExecutableError as error:
            raise ClaudeControllerError("CONTROLLER_SANDBOX_CHANGED") from error
        try:
            spec = sandbox_adapter(self._sandbox_executable).compile(request)
        except (OSError, ValueError, SandboxHostError) as error:
            raise ClaudeControllerError("CONTROLLER_SANDBOX_INVALID") from error
        if spec.argv[-len(command) :] != command:
            raise ClaudeControllerError("CONTROLLER_EXECUTABLE_MISMATCH")
        return await self._launch(
            process.ControllerLaunchSpec(
                spec.argv,
                spec.cwd,
                dict(spec.parent_env),
                source,
                timeout_seconds,
                1024 * 1024,
            )
        )

    def _validate_image(self) -> None:
        try:
            revalidate_claude(self._descriptor, self._source_environment)
        except ProviderExecutableError as error:
            _raise_provider_error(error)


def _environment(source: Mapping[str, str], home: Path, scratch: Path) -> dict[str, str]:
    return {
        **{key: source[key] for key in _AUTH_ENVIRONMENT if key in source},
        "HOME": source.get("HOME", str(home / "home")),
        "PATH": source.get("PATH", "/usr/bin:/bin"),
        "LANG": source.get("LANG", "C.UTF-8"),
        "LC_ALL": source.get("LC_ALL", "C.UTF-8"),
        "TMPDIR": str(scratch),
    }


def _canonical_executable(path: Path) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ClaudeControllerError("CONTROLLER_EXECUTABLE_UNAVAILABLE") from error
    if (
        not path.is_absolute()
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or resolved != path
        or not os.access(path, os.X_OK)
    ):
        raise ClaudeControllerError("CONTROLLER_EXECUTABLE_UNAVAILABLE")
    return resolved


def _canonical_project(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ClaudeControllerError("CONTROLLER_PROJECT_INVALID") from error
    if not resolved.is_dir():
        raise ClaudeControllerError("CONTROLLER_PROJECT_INVALID")
    return resolved


def _runtime_roots(executable: Path) -> tuple[Path, ...]:
    defaults = ssl.get_default_verify_paths()
    candidates = (
        executable.parent,
        *(Path(value).parent for value in (defaults.cafile, defaults.openssl_cafile) if value),
        *(Path(value) for value in (defaults.capath, defaults.openssl_capath) if value),
    )
    return tuple(dict.fromkeys(path.resolve() for path in candidates if path.is_dir()))


def _read_decision(raw: bytes, context: cb.ControllerContext) -> cb.ControllerDecision:
    if len(raw) > 1024 * 1024:
        raise ClaudeControllerError("CONTROLLER_RESULT_OVERSIZED")
    try:
        envelope: JsonValue = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ClaudeControllerError) as error:
        raise ClaudeControllerError("CONTROLLER_RESULT_INVALID") from error
    if type(envelope) is not dict or type(envelope.get("structured_output")) is not dict:
        raise ClaudeControllerError("CONTROLLER_RESULT_INVALID")
    try:
        return cb.parse_controller_wire_decision(
            canonical_json(envelope["structured_output"]), context
        )
    except cb.ControllerBackendError as error:
        raise ClaudeControllerError("CONTROLLER_RESULT_INVALID") from error


def _unique_json_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    value: dict[str, JsonValue] = {}
    for key, item in pairs:
        if key in value:
            raise ClaudeControllerError("CONTROLLER_RESULT_INVALID")
        value[key] = item
    return value


def _raise_provider_error(error: ProviderExecutableError) -> Never:
    if error.code == "CONTROLLER_VERSION_UNSUPPORTED":
        raise ClaudeControllerError(error.code) from error
    if error.code == "CLAUDE_EXECUTABLE_UNAVAILABLE":
        raise ClaudeControllerError("CONTROLLER_EXECUTABLE_UNAVAILABLE") from error
    if error.code == "PROVIDER_VERSION_UNAVAILABLE":
        raise ClaudeControllerError("CONTROLLER_VERSION_UNAVAILABLE") from error
    raise ClaudeControllerError("CONTROLLER_IMAGE_CHANGED") from error
