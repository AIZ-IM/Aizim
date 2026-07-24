from __future__ import annotations

import os
import select
import signal
import subprocess
import time
from contextlib import suppress

from .process_io import READ_CHUNK_SIZE, ProcessOutputLimitError, ProcessPipeError


def exchange_bounded(
    process: subprocess.Popen[bytes],
    source: bytes,
    timeout_seconds: float,
    output_limit: int,
) -> tuple[int, bytes, bytes]:
    stdin, stdout, stderr = process.stdin, process.stdout, process.stderr
    if stdin is None or stdout is None or stderr is None:
        raise ProcessPipeError
    output, errors = bytearray(), bytearray()
    descriptors = {stdout.fileno(): output, stderr.fileno(): errors}
    pending, deadline = memoryview(source), time.monotonic() + timeout_seconds
    for stream in (stdin, stdout, stderr):
        os.set_blocking(stream.fileno(), False)
    if not pending:
        stdin.close()
    while descriptors or process.poll() is None:
        if (remaining := deadline - time.monotonic()) <= 0:
            raise TimeoutError
        ready, writable, _ = select.select(
            tuple(descriptors), () if not pending else (stdin.fileno(),), (), remaining
        )
        for descriptor in ready:
            chunk = os.read(descriptor, READ_CHUNK_SIZE)
            if chunk:
                descriptors[descriptor].extend(chunk)
            else:
                descriptors.pop(descriptor)
            if len(output) + len(errors) > output_limit:
                raise ProcessOutputLimitError
        if writable and not (pending := pending[os.write(stdin.fileno(), pending) :]):
            stdin.close()
    return process.returncode, bytes(output), bytes(errors)


def kill_and_reap(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    while _process_group_exists(process.pid):
        time.sleep(0.01)


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
