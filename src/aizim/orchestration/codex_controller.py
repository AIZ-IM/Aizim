from __future__ import annotations

import os
import ssl
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Never

from aizim.agents import BackendIdentity
from aizim.agents.codex_events import transport_usage
from aizim.agents.macos_sandbox import SandboxHostError
from aizim.agents.platform_sandbox import sandbox_adapter
from aizim.agents.sandbox import ProviderEnvironmentPolicy, SandboxRequest
from aizim.runtime.provider_executables import (
    ProviderExecutableError,
    ResolvedExecutable,
    codex_runtime_root,
    revalidate_codex,
)

from .controller_backend import (
    ControllerBackendError,
    ControllerContext,
    ControllerDecision,
    controller_context_bytes,
    parse_controller_wire_decision,
)
from .controller_process import (
    ControllerLauncher,
    ControllerLaunchError,
    ControllerLaunchOutcome,
    ControllerLaunchSpec,
    launch_controller_process,
    private_workspace,
)
from .controller_transport import codex_tool_policy, prepare_auth

_OUTPUT_LIMIT: Final = 1024 * 1024
_SCHEMA_PATH: Final = Path(__file__).with_name("controller_wire.schema.json").resolve()
_AUTH_ENVIRONMENT: Final = frozenset(
    {
        "OPENAI_API_KEY",
        "CODEX_HOME",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)


class CodexControllerError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CodexControllerBackend:
    def __init__(
        self,
        executable: ResolvedExecutable,
        model: str | None,
        project_root: Path,
        parent_environment: Mapping[str, str],
        launch: ControllerLauncher = launch_controller_process,
    ) -> None:
        try:
            revalidate_codex(executable, parent_environment)
        except ProviderExecutableError as error:
            _raise_provider_error(error)
        self._descriptor = executable
        self._executable = executable.path
        self._project = _canonical_project(project_root)
        if model is not None and (type(model) is not str or not model):
            raise CodexControllerError("CONTROLLER_MODEL_INVALID")
        self._model = model
        self.last_usage: dict[str, int] | None = None
        self._source_environment = dict(parent_environment)
        self._launch = launch
        self._image_hash = executable.sha256
        self._identity = BackendIdentity("codex", executable.version, executable.sha256)

    @property
    def identity(self) -> BackendIdentity:
        return self._identity

    async def preflight(self) -> None:
        self._validate_image()
        with private_workspace("aizim-controller-") as roots:
            private, _view, _scratch = roots
            outcome = await self._run(
                private,
                (str(self._executable), "login", "status"),
                b"",
                10.0,
            )
            if outcome.exit_code != 0:
                raise CodexControllerError("CONTROLLER_LOGIN_FAILED")

    async def plan(self, context: ControllerContext) -> ControllerDecision:
        try:
            return await self._plan(context)
        except CodexControllerError as error:
            raise ControllerBackendError("CONTROLLER_DECISION_INVALID") from error

    async def _plan(self, context: ControllerContext) -> ControllerDecision:
        self.last_usage = None
        self._validate_image()
        with private_workspace("aizim-controller-") as roots:
            private, view, scratch = roots
            final = scratch / "controller-final.json"
            schema = scratch / "controller-schema.json"
            schema.write_bytes(_SCHEMA_PATH.read_bytes())
            model = () if self._model is None else ("--model", self._model)
            command = (
                str(self._executable),
                "-c",
                'shell_environment_policy.inherit="none"',
                *codex_tool_policy(
                    view, scratch, self._project, codex_runtime_root(self._executable)
                ),
                "--strict-config",
                "exec",
                "--ignore-user-config",
                "--ignore-rules",
                "--ephemeral",
                "--skip-git-repo-check",
                "--json",
                "--output-schema",
                str(schema),
                "--output-last-message",
                str(final),
                "-C",
                str(view),
                *model,
                "-",
            )
            try:
                outcome = await self._run(
                    private,
                    command,
                    controller_context_bytes(context),
                    context.max_timeout_seconds,
                    network=True,
                )
            except ControllerLaunchError as error:
                if error.code == "CONTROLLER_TIMEOUT":
                    raise TimeoutError from error
                raise ControllerBackendError("CONTROLLER_DECISION_INVALID") from error
            self.last_usage = transport_usage(outcome.stdout)
            if outcome.exit_code != 0:
                raise ControllerBackendError("CONTROLLER_DECISION_INVALID")
            return _read_decision(final, context)

    async def _run(
        self,
        private: Path,
        command: tuple[str, ...],
        source: bytes,
        timeout_seconds: float,
        *,
        network: bool = False,
    ) -> ControllerLaunchOutcome:
        view, scratch = private / "aizim-view-empty", private / "aizim-scratch-data"
        environment = _environment(self._source_environment, private, scratch)
        auth_key, auth_root = prepare_auth("codex", self._source_environment, scratch)
        environment[auth_key] = str(auth_root)
        environment["TMPDIR"] = str(scratch / "tmp")
        if network:
            environment["TMPDIR"] = str(scratch / "client-tmp")
        request = SandboxRequest(
            self._project,
            view,
            scratch,
            command,
            environment,
            _runtime_roots(self._executable),
            ProviderEnvironmentPolicy.FILTERED_PARENT,
        )
        try:
            spec = sandbox_adapter(self._executable).compile(request)
        except (OSError, ValueError, SandboxHostError) as error:
            raise CodexControllerError("CONTROLLER_SANDBOX_INVALID") from error
        if spec.argv[-len(command) :] != command:
            raise CodexControllerError("CONTROLLER_EXECUTABLE_MISMATCH")
        # API transport stays in the attested CLI parent. The command's explicit
        # native permission profile confines model tools; nesting the CLI itself
        # inside another Codex sandbox prevents its filesystem helpers starting.
        return await self._launch(
            ControllerLaunchSpec(
                command if network else spec.argv,
                spec.cwd,
                dict(spec.parent_env),
                source,
                timeout_seconds,
                _OUTPUT_LIMIT,
            )
        )

    def _validate_image(self) -> None:
        try:
            revalidate_codex(self._descriptor, self._source_environment)
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
        raise CodexControllerError("CONTROLLER_EXECUTABLE_UNAVAILABLE") from error
    if (
        not path.is_absolute()
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or resolved != path
        or not os.access(path, os.X_OK)
    ):
        raise CodexControllerError("CONTROLLER_EXECUTABLE_UNAVAILABLE")
    return resolved


def _canonical_project(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise CodexControllerError("CONTROLLER_PROJECT_INVALID") from error
    if not resolved.is_dir():
        raise CodexControllerError("CONTROLLER_PROJECT_INVALID")
    return resolved


def _runtime_roots(executable: Path) -> tuple[Path, ...]:
    defaults = ssl.get_default_verify_paths()
    candidates = (
        codex_runtime_root(executable),
        *(Path(value).parent for value in (defaults.cafile, defaults.openssl_cafile) if value),
        *(Path(value) for value in (defaults.capath, defaults.openssl_capath) if value),
    )
    return tuple(dict.fromkeys(path.resolve() for path in candidates if path.is_dir()))


def _read_decision(final: Path, context: ControllerContext) -> ControllerDecision:
    try:
        size = final.stat().st_size
    except OSError as error:
        raise CodexControllerError("CONTROLLER_RESULT_MISSING") from error
    if size > _OUTPUT_LIMIT:
        raise CodexControllerError("CONTROLLER_RESULT_OVERSIZED")
    try:
        raw = final.read_bytes()
        return parse_controller_wire_decision(raw, context)
    except (OSError, ControllerBackendError) as error:
        raise CodexControllerError("CONTROLLER_RESULT_INVALID") from error


def _raise_provider_error(error: ProviderExecutableError) -> Never:
    if error.code == "UNSUPPORTED_CODEX_VERSION":
        raise CodexControllerError("CONTROLLER_VERSION_UNSUPPORTED") from error
    if error.code == "CODEX_EXECUTABLE_UNAVAILABLE":
        raise CodexControllerError("CONTROLLER_EXECUTABLE_UNAVAILABLE") from error
    if error.code == "PROVIDER_VERSION_UNAVAILABLE":
        raise CodexControllerError("CONTROLLER_VERSION_UNAVAILABLE") from error
    raise CodexControllerError("CONTROLLER_IMAGE_CHANGED") from error
