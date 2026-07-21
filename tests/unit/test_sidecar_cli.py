from __future__ import annotations

import inspect
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from aizim.cli import main as cli_main
from aizim.gateway import sidecar


@pytest.mark.parametrize("entrypoint", ("console", "hidden"))
@pytest.mark.parametrize("argument_style", ("separate", "equals"))
def test_consumed_session_id_is_scrubbed_from_python_references(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    entrypoint: str,
    argument_style: str,
) -> None:
    raw_session_id = "session-sensitive-7"
    socket_path = tmp_path / "gateway.sock"
    observed_socket: Path | None = None
    observed_session_id = ""
    observed_process_argv = ""
    observed_source_locals: list[str] = []

    def fake_run(received_socket: Path, received_session_id: str) -> int:
        nonlocal observed_process_argv
        nonlocal observed_session_id
        nonlocal observed_socket
        nonlocal observed_source_locals
        frame = inspect.currentframe()
        source_locals: list[str] = []
        while frame is not None:
            frame = frame.f_back
            if frame is not None and "/src/aizim/" in frame.f_code.co_filename:
                source_locals.append(repr(frame.f_locals))
        observed_socket = received_socket
        observed_session_id = received_session_id
        observed_process_argv = repr(sys.argv)
        observed_source_locals = source_locals
        return 0

    session_arguments = (
        ["--session-id", raw_session_id]
        if argument_style == "separate"
        else [f"--session-id={raw_session_id}"]
    )
    entry: Callable[[], int]
    if entrypoint == "console":
        monkeypatch.setattr(sidecar, "run_gateway_sidecar", fake_run)
        process_argv = [
            "aizim-gateway-sidecar",
            "--broker-socket",
            str(socket_path),
            *session_arguments,
        ]
        entry = sidecar.main
    else:
        monkeypatch.setattr(cli_main, "run_gateway_sidecar", fake_run)
        process_argv = [
            "aizim",
            "gateway",
            "sidecar",
            "--broker-socket",
            str(socket_path),
            *session_arguments,
        ]
        entry = cli_main.main
    monkeypatch.setattr(sys, "argv", process_argv)

    assert entry() == 0
    assert (observed_socket, observed_session_id) == (socket_path, raw_session_id)
    assert raw_session_id not in observed_process_argv
    assert all(raw_session_id not in local for local in observed_source_locals)
