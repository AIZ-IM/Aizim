from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from aizim.async_lifecycle import await_cleanup

from .codex_events import CodexEventError, TransportEventHasher, parse_final_message
from .process_io import READ_CHUNK_SIZE, drain_process

_JSONL_LINE_LIMIT: Final = 4 * 1024 * 1024
_STDERR_LIMIT: Final = 256 * 1024
_TERMINATE_GRACE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class AgentLaunchError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class CodexLaunchSpec:
    argv: tuple[str, ...]
    cwd: Path
    parent_env: Mapping[str, str] = field(repr=False)
    shell_env: Mapping[str, str]
    stdin: bytes = field(repr=False)
    timeout_seconds: float
    final_message_path: Path
    output_schema_path: Path


@dataclass(frozen=True, slots=True)
class CodexLaunchOutcome:
    status: Literal["submitted", "abstained", "failed"]
    summary: str
    transport_event_hash: str
    final_message_hash: str
    exit_code: int


async def launch_codex(spec: CodexLaunchSpec) -> CodexLaunchOutcome:
    if os.path.lexists(spec.final_message_path):
        raise AgentLaunchError("CODEX_RESULT_PATH_OCCUPIED")
    process = await asyncio.create_subprocess_exec(
        *spec.argv,
        cwd=spec.cwd,
        env=dict(spec.parent_env),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=READ_CHUNK_SIZE,
    )
    hasher = TransportEventHasher()
    try:
        await asyncio.wait_for(
            _communicate(process, spec.stdin, hasher), timeout=spec.timeout_seconds
        )
    except TimeoutError as error:
        await _finish_reaping(process, graceful=True)
        raise AgentLaunchError("CODEX_TIMEOUT") from error
    except CodexEventError as error:
        await _finish_reaping(process, graceful=False)
        raise AgentLaunchError(str(error)) from error
    except AgentLaunchError:
        await _finish_reaping(process, graceful=False)
        raise
    except BaseException:
        await _finish_reaping(process, graceful=False)
        raise
    if process.returncode != 0:
        raise AgentLaunchError("CODEX_PROCESS_FAILED")
    try:
        status, summary, final_hash = parse_final_message(spec.final_message_path)
    except CodexEventError as error:
        raise AgentLaunchError(str(error)) from error
    return CodexLaunchOutcome(
        status,
        summary,
        hasher.hexdigest(),
        final_hash,
        process.returncode,
    )


async def _communicate(
    process: asyncio.subprocess.Process,
    source: bytes,
    hasher: TransportEventHasher,
) -> None:
    stdin, stdout, stderr = process.stdin, process.stdout, process.stderr
    if stdin is None or stdout is None or stderr is None:
        raise AgentLaunchError("CODEX_PIPE_FAILED")
    tasks = (
        asyncio.create_task(_write_input(stdin, source)),
        asyncio.create_task(_read_jsonl(stdout, hasher)),
        asyncio.create_task(_read_stderr(stderr)),
        asyncio.create_task(process.wait()),
    )
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _write_input(writer: asyncio.StreamWriter, source: bytes) -> None:
    try:
        writer.write(source)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        return
    finally:
        writer.close()


async def _read_jsonl(reader: asyncio.StreamReader, hasher: TransportEventHasher) -> None:
    pending = bytearray()
    while chunk := await reader.read(READ_CHUNK_SIZE):
        pending.extend(chunk)
        while (newline := pending.find(b"\n")) >= 0:
            line = bytes(pending[:newline])
            del pending[: newline + 1]
            if len(line) > _JSONL_LINE_LIMIT:
                raise AgentLaunchError("CODEX_STDOUT_LIMIT")
            if line:
                hasher.add(line)
        if len(pending) > _JSONL_LINE_LIMIT:
            raise AgentLaunchError("CODEX_STDOUT_LIMIT")
    if pending:
        hasher.add(bytes(pending))


async def _read_stderr(reader: asyncio.StreamReader) -> None:
    length = 0
    while chunk := await reader.read(READ_CHUNK_SIZE):
        length += len(chunk)
        if length > _STDERR_LIMIT:
            raise AgentLaunchError("CODEX_STDERR_LIMIT")


async def _terminate_and_reap(process: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(drain_process(process), timeout=_TERMINATE_GRACE_SECONDS)
    except TimeoutError:
        await _kill_and_reap(process)


async def _finish_reaping(process: asyncio.subprocess.Process, *, graceful: bool) -> None:
    cleanup = _terminate_and_reap(process) if graceful else _kill_and_reap(process)
    await await_cleanup(asyncio.create_task(cleanup))


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await drain_process(process)
