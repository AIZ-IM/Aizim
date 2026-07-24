from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Never

from aizim.agents.launcher import (
    AgentLaunchError,
    HostCommandSpec,
    launch_host_command,
    run_host_command,
)
from aizim.agents.process_io import (
    ProcessOutputLimitError,
    ProcessPipeError,
)


@dataclass(frozen=True, slots=True)
class ControllerLaunchSpec:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str] = field(repr=False)
    stdin: bytes = field(repr=False)
    timeout_seconds: float
    output_limit: int


@dataclass(frozen=True, slots=True)
class ControllerLaunchOutcome:
    stdout: bytes = field(repr=False)
    stderr_hash: str
    exit_code: int


class ControllerLaunchError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


type ControllerLauncher = Callable[[ControllerLaunchSpec], Awaitable[ControllerLaunchOutcome]]


@contextmanager
def private_workspace(prefix: str) -> Iterator[tuple[Path, Path, Path]]:
    with tempfile.TemporaryDirectory(prefix=prefix) as name:
        private = Path(name)
        private.chmod(0o700)
        view, scratch = private / "aizim-view-empty", private / "aizim-scratch-data"
        for child in (view, scratch, private / "home"):
            child.mkdir(mode=0o700)
        yield private, view, scratch


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


def run_controller_host_command(
    argv: tuple[str, ...],
    environment: dict[str, str],
    output_limit: int,
) -> bytes:
    try:
        outcome = run_host_command(
            HostCommandSpec(
                argv,
                Path(tempfile.gettempdir()).resolve(),
                environment,
                output_limit=output_limit,
            )
        )
    except AgentLaunchError as error:
        _raise_controller_error(error)
    if outcome.returncode != 0:
        raise ControllerLaunchError("CONTROLLER_PROCESS_FAILED")
    return outcome.stdout


async def launch_controller_process(
    spec: ControllerLaunchSpec,
) -> ControllerLaunchOutcome:
    try:
        outcome = await launch_host_command(
            HostCommandSpec(
                spec.argv,
                spec.cwd,
                spec.env,
                spec.stdin,
                spec.timeout_seconds,
                spec.output_limit,
            )
        )
    except AgentLaunchError as error:
        _raise_controller_error(error)
    if outcome.returncode != 0:
        raise ControllerLaunchError("CONTROLLER_PROCESS_FAILED")
    return ControllerLaunchOutcome(
        outcome.stdout,
        hashlib.sha256(outcome.stderr).hexdigest(),
        outcome.returncode,
    )


def _raise_controller_error(error: AgentLaunchError) -> Never:
    cause = error.__cause__
    if isinstance(cause, TimeoutError):
        raise ControllerLaunchError("CONTROLLER_TIMEOUT") from error
    if isinstance(cause, ProcessOutputLimitError):
        raise ControllerLaunchError("CONTROLLER_OUTPUT_LIMIT") from error
    if isinstance(cause, ProcessPipeError):
        raise ControllerLaunchError("CONTROLLER_PIPE_FAILED") from error
    raise ControllerLaunchError("CONTROLLER_PROCESS_FAILED") from error
