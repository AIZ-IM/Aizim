from __future__ import annotations

import asyncio
from typing import Final

READ_CHUNK_SIZE: Final = 64 * 1024


class ProcessOutputLimitError(RuntimeError):
    pass


class ProcessPipeError(RuntimeError):
    pass


async def communicate_bounded(
    process: asyncio.subprocess.Process, source: bytes, output_limit: int
) -> tuple[bytes, bytes]:
    stdin, stdout, stderr = process.stdin, process.stdout, process.stderr
    if stdin is None or stdout is None or stderr is None:
        raise ProcessPipeError
    input_task = asyncio.create_task(_write_input(stdin, source))
    stdout_task = asyncio.create_task(_read_bounded(stdout, output_limit))
    stderr_task = asyncio.create_task(_read_bounded(stderr, output_limit))
    wait_task = asyncio.create_task(process.wait())
    try:
        await asyncio.gather(input_task, stdout_task, stderr_task, wait_task)
        return stdout_task.result(), stderr_task.result()
    finally:
        for task in (input_task, stdout_task, stderr_task, wait_task):
            task.cancel()
        await asyncio.gather(
            input_task, stdout_task, stderr_task, wait_task, return_exceptions=True
        )


async def drain_process(process: asyncio.subprocess.Process) -> None:
    streams = tuple(stream for stream in (process.stdout, process.stderr) if stream is not None)
    await asyncio.gather(
        *(_discard(stream) for stream in streams), process.wait(), return_exceptions=True
    )


async def _write_input(writer: asyncio.StreamWriter, source: bytes) -> None:
    try:
        writer.write(source)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        writer.close()


async def _read_bounded(stream: asyncio.StreamReader, output_limit: int) -> bytes:
    output = bytearray()
    while chunk := await stream.read(min(READ_CHUNK_SIZE, output_limit - len(output) + 1)):
        output.extend(chunk)
        if len(output) > output_limit:
            raise ProcessOutputLimitError
    return bytes(output)


async def _discard(stream: asyncio.StreamReader) -> None:
    while await stream.read(READ_CHUNK_SIZE):
        pass
