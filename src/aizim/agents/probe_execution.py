from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress
from pathlib import Path
from typing import Final

from .process_io import (
    READ_CHUNK_SIZE,
    ProcessOutputLimitError,
    ProcessPipeError,
    communicate_bounded,
    drain_process,
)
from .sandbox import (
    ProbeOperation,
    ProbeReport,
    ProbeRequest,
    SandboxAdapter,
    SandboxLaunchSpec,
    SandboxRequest,
    append_probe_event,
    parse_probe_attempts,
    probe_document,
    protected_asset_digests,
)

OUTPUT_LIMIT: Final = 256 * 1024


class SandboxProbeError(RuntimeError):
    pass


async def execute_probe(
    adapter: SandboxAdapter,
    request: ProbeRequest,
    *,
    codex_version: str,
    sandbox_executable: str,
) -> ProbeReport:
    protected_before = protected_asset_digests(request)
    logical_before = request.event_sink.logical_digest()
    document = probe_document(request)
    command = ("/usr/bin/python3", "-I", "-B", "-", document)
    spec = adapter.compile(
        SandboxRequest(
            request.project_root,
            request.view_root,
            request.scratch_root,
            command,
            request.parent_env,
            request.runtime_read_roots,
        )
    )
    stdout, stderr = await run_probe_process(spec, request.timeout_seconds)
    secret = request.parent_env.get(request.secret_environment_name)
    if secret and (secret.encode() in stdout or secret.encode() in stderr):
        raise SandboxProbeError("PROBE_OUTPUT_CONTAINED_SECRET")
    try:
        attempts = parse_probe_attempts(stdout)
    except ValueError as error:
        raise SandboxProbeError("INVALID_PROBE_OUTPUT") from error
    for attempt in attempts:
        append_probe_event(request, attempt, spec.policy_hash)
    logical_after = request.event_sink.logical_digest()
    protected_after = protected_asset_digests(request)
    expected = tuple(ProbeOperation)
    verdicts_match = all(
        attempt.verdict == ("denied" if index < 9 else "allowed")
        and attempt.reason_code
        == ("SANDBOX_ENFORCED" if index < 9 else "OPERATION_ALLOWED")
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
        platform_id=adapter.platform_id,
        passed=passed,
        codex_version=codex_version,
        sandbox_executable=sandbox_executable,
        policy_hash=spec.policy_hash,
        attempts=attempts,
        unexpected_allows=unexpected_allows,
        logical_digest_before=logical_before,
        logical_digest_after=logical_after,
    )


async def run_probe_process(
    spec: SandboxLaunchSpec,
    timeout_seconds: float,
) -> tuple[bytes, bytes]:
    source = Path(__file__).with_name("attack_probe.py").read_bytes()
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
    try:
        stdout, stderr = await asyncio.wait_for(
            communicate_bounded(process, source, OUTPUT_LIMIT),
            timeout=timeout_seconds,
        )
    except BaseException as error:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await drain_process(process)
        if isinstance(error, TimeoutError):
            raise SandboxProbeError("PROBE_TIMEOUT") from error
        if isinstance(error, ProcessOutputLimitError):
            raise SandboxProbeError("PROBE_OUTPUT_LIMIT") from error
        if isinstance(error, ProcessPipeError):
            raise SandboxProbeError("PROBE_PIPE_FAILED") from error
        raise
    if process.returncode != 0:
        raise SandboxProbeError("PROBE_PROCESS_FAILED")
    return stdout, stderr
