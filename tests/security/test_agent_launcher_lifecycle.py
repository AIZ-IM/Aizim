from __future__ import annotations

import asyncio
import os
import signal
import time
from contextlib import suppress
from pathlib import Path

import pytest

from aizim.agents import launcher
from aizim.agents.launcher import (
    AgentLaunchError,
    CodexLaunchSpec,
    HostCommandSpec,
    launch_codex,
    run_host_command,
)


async def _wait_for_file(path: Path) -> None:
    while not path.exists():
        await asyncio.sleep(0.01)


async def _wait_for_process_exit(pid: int) -> None:
    for _attempt in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"process {pid} remained alive")


@pytest.mark.parametrize("returncode", [0, 7])
def test_host_runner_reaps_fd_detached_descendant_before_return(
    returncode: int, tmp_path: Path
) -> None:
    descendant_file = tmp_path / "descendant"
    command = tmp_path / "host-command.py"
    command.write_text(
        "import os, pathlib, time\n"
        f"descendant = pathlib.Path({str(descendant_file)!r})\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    descendant.write_text(str(os.getpid()))\n"
        "    for descriptor in (0, 1, 2): os.close(descriptor)\n"
        "    while True: time.sleep(1)\n"
        "while not descendant.exists(): time.sleep(0.01)\n"
        "print('parent-output', flush=True)\n"
        f"raise SystemExit({returncode})\n"
    )
    outcome = run_host_command(
        HostCommandSpec(
            ("/usr/bin/python3", str(command)),
            tmp_path,
            {"PATH": "/usr/bin:/bin"},
        )
    )
    descendant_pid = int(descendant_file.read_text())
    try:
        assert outcome.returncode == returncode
        assert outcome.stdout == b"parent-output\n"
        assert outcome.stderr == b""
        with pytest.raises(ProcessLookupError):
            os.kill(descendant_pid, 0)
    finally:
        with suppress(ProcessLookupError):
            os.killpg(os.getpgid(descendant_pid), signal.SIGKILL)
        for _attempt in range(100):
            try:
                os.kill(descendant_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)


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


async def test_timeout_kills_descendant_after_direct_child_exits_on_term(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent_file, descendant_file = tmp_path / "parent", tmp_path / "descendant"
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/usr/bin/python3\n"
        "import os, pathlib, signal, time\n"
        f"parent = pathlib.Path({str(parent_file)!r})\n"
        f"descendant = pathlib.Path({str(descendant_file)!r})\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    descendant.write_text(str(os.getpid()))\n"
        "    for descriptor in (0, 1, 2):\n"
        "        os.close(descriptor)\n"
        "    while True: time.sleep(1)\n"
        "while not descendant.exists(): time.sleep(0.01)\n"
        "signal.signal(signal.SIGTERM, lambda *_: os._exit(0))\n"
        "parent.write_text(str(os.getpid()))\n"
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
    monkeypatch.setattr(launcher, "_TERMINATE_GRACE_SECONDS", 0.1)
    run = asyncio.create_task(launch_codex(spec))
    try:
        await asyncio.wait_for(_wait_for_file(parent_file), 0.8)
        await asyncio.wait_for(_wait_for_file(descendant_file), 0.8)
        with pytest.raises(AgentLaunchError, match="CODEX_TIMEOUT"):
            await run
        await _wait_for_process_exit(int(parent_file.read_text()))
        await _wait_for_process_exit(int(descendant_file.read_text()))
    finally:
        if not run.done():
            run.cancel()
        if parent_file.exists():
            with suppress(ProcessLookupError):
                os.killpg(int(parent_file.read_text()), signal.SIGKILL)
        await asyncio.gather(run, return_exceptions=True)
