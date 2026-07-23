from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from aizim.agents import probe_execution, process_io
from aizim.agents.probe_execution import SandboxProbeError
from aizim.agents.sandbox import SandboxLaunchSpec


def launch_spec(tmp_path: Path, argv: tuple[str, ...]) -> SandboxLaunchSpec:
    return SandboxLaunchSpec(
        platform_id="darwin",
        argv=argv,
        cwd=tmp_path,
        parent_env={},
        shell_env={},
        view_root=tmp_path,
        scratch_root=tmp_path,
        profile_id="test",
        policy_hash="0" * 64,
    )


async def test_probe_output_reader_enforces_limit_while_streaming() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"x" * (probe_execution.OUTPUT_LIMIT + 1))
    reader.feed_eof()

    with pytest.raises(process_io.ProcessOutputLimitError):
        await process_io._read_bounded(reader, probe_execution.OUTPUT_LIMIT)


async def test_execute_kills_process_group_at_output_limit(tmp_path: Path) -> None:
    pid_file = tmp_path / "probe.pid"
    script = (
        "import os,pathlib,sys,time;"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
        f"sys.stdout.buffer.write(b'x'*{probe_execution.OUTPUT_LIMIT * 8});"
        "sys.stdout.flush();time.sleep(30)"
    )

    with pytest.raises(SandboxProbeError, match="PROBE_OUTPUT_LIMIT"):
        await asyncio.wait_for(
            probe_execution.run_probe_process(
                launch_spec(tmp_path, ("/usr/bin/python3", "-c", script, str(pid_file))),
                1.0,
            ),
            timeout=2.0,
        )

    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


async def test_execute_reads_probe_source_before_spawning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spawned = False

    async def unexpected_spawn(*args: object, **kwargs: object) -> None:
        nonlocal spawned
        spawned = True

    def unavailable_source(_path: Path) -> bytes:
        raise OSError("probe source unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_spawn)
    monkeypatch.setattr(Path, "read_bytes", unavailable_source)

    with pytest.raises(OSError, match="probe source unavailable"):
        await probe_execution.run_probe_process(
            launch_spec(tmp_path, ("/usr/bin/false",)),
            1.0,
        )

    assert not spawned
