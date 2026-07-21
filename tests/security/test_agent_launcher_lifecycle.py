from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress
from pathlib import Path

import pytest

from aizim.agents import launcher
from aizim.agents.launcher import AgentLaunchError, CodexLaunchSpec, launch_codex


async def _wait_for_file(path: Path) -> None:
    while not path.exists():
        await asyncio.sleep(0.01)


async def test_cancel_during_term_grace_still_kills_and_reaps_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pid_file, term_file = tmp_path / "pid", tmp_path / "term"
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/usr/bin/python3\n"
        "import os, pathlib, signal, time\n"
        f"pid = pathlib.Path({str(pid_file)!r})\n"
        f"term = pathlib.Path({str(term_file)!r})\n"
        "signal.signal(signal.SIGTERM, lambda *_: term.write_text('term'))\n"
        "pid.write_text(str(os.getpid()))\n"
        "while True: time.sleep(1)\n"
    )
    executable.chmod(0o700)
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    spec = CodexLaunchSpec(
        (str(executable),),
        tmp_path,
        {"PATH": "/usr/bin:/bin"},
        {"PATH": "/usr/bin"},
        b"fixture",
        1.0,
        tmp_path / "result.json",
        schema,
    )
    monkeypatch.setattr(launcher, "_TERMINATE_GRACE_SECONDS", 0.5)
    run = asyncio.create_task(launch_codex(spec))
    await asyncio.wait_for(_wait_for_file(pid_file), 1)
    await asyncio.wait_for(_wait_for_file(term_file), 1)
    pid = int(pid_file.read_text())
    try:
        run.cancel("cancel-during-term-grace")
        with pytest.raises(AgentLaunchError, match="CODEX_TIMEOUT"):
            await run
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if not run.done():
            run.cancel()
        with suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
        await asyncio.gather(run, return_exceptions=True)
