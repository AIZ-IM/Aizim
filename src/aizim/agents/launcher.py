from __future__ import annotations

import asyncio  # ANYIO_OK: subprocess lifecycle uses asyncio primitives
import os
import signal
import subprocess
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from aizim.async_lifecycle import await_cleanup

from .process_io import (
    READ_CHUNK_SIZE,
    ProcessOutputLimitError,
    ProcessPipeError,
    communicate_bounded,
    drain_process,
)
from .sync_process_io import exchange_bounded, kill_and_reap


class AgentLaunchError(RuntimeError):
    @property
    def reason(self) -> str:
        return str(self)


class HostCommandSpecError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HostCommandSpec:
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str] = field(repr=False)
    stdin: bytes = field(default=b"", repr=False)
    timeout_seconds: float = 10.0
    output_limit: int = 64 * 1024

    def __post_init__(self) -> None:
        if not self.argv or not Path(self.argv[0]).is_absolute():
            raise HostCommandSpecError("host command requires an absolute executable")
        if not 0 < self.timeout_seconds < float("inf"):
            raise HostCommandSpecError("host command timeout must be finite and positive")
        if type(self.output_limit) is not int or self.output_limit <= 0:
            raise HostCommandSpecError("host command output limit must be positive")


@dataclass(frozen=True, slots=True)
class HostCommandOutcome:
    returncode: int
    stdout: bytes
    stderr: bytes


def run_host_command(spec: HostCommandSpec) -> HostCommandOutcome:
    process = subprocess.Popen(
        spec.argv,
        cwd=spec.cwd,
        env=dict(spec.environment),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        returncode, stdout, stderr = exchange_bounded(
            process, spec.stdin, spec.timeout_seconds, spec.output_limit
        )
    except (TimeoutError, ProcessOutputLimitError, ProcessPipeError) as error:
        raise AgentLaunchError("HOST_COMMAND_FAILED") from error
    except OSError:
        raise AgentLaunchError("HOST_COMMAND_FAILED") from ProcessPipeError()
    finally:
        kill_and_reap(process)
    return HostCommandOutcome(returncode, stdout, stderr)


async def launch_host_command(spec: HostCommandSpec) -> HostCommandOutcome:
    process = await asyncio.create_subprocess_exec(
        *spec.argv,
        cwd=spec.cwd,
        env=dict(spec.environment),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=READ_CHUNK_SIZE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            communicate_bounded(process, spec.stdin, spec.output_limit),
            timeout=spec.timeout_seconds,
        )
    except (TimeoutError, ProcessOutputLimitError, ProcessPipeError) as error:
        await _finish_reaping(process)
        raise AgentLaunchError("HOST_COMMAND_FAILED") from error
    except BaseException:  # BROAD_EXCEPT_OK: subprocess resource boundary
        await _finish_reaping(process)
        raise
    if len(stdout) + len(stderr) > spec.output_limit:
        await _finish_reaping(process)
        raise AgentLaunchError("HOST_COMMAND_FAILED") from ProcessOutputLimitError()
    await _finish_reaping(process)
    return HostCommandOutcome(process.returncode or 0, stdout, stderr)


async def _drain_process_group(process: asyncio.subprocess.Process) -> None:
    await drain_process(process)
    while _process_group_exists(process.pid):
        await asyncio.sleep(0.01)


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _finish_reaping(process: asyncio.subprocess.Process) -> None:
    await await_cleanup(asyncio.create_task(_kill_and_reap(process)))


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await _drain_process_group(process)
