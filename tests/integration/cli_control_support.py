from __future__ import annotations

import asyncio  # noqa: ANYIO_OK -- drives the asyncio state RPC client
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Literal, assert_never

from aizim.domain.serialization import JsonValue
from aizim.state.operations import RpcRequest, RpcSuccess
from aizim.state.rpc import rpc_call

FIXTURE = Path(__file__).parents[1] / "fixtures" / "minimal_lean"
type PublicControlCommand = Literal["configure", "register", "assign"]
type StaticOwnershipState = Literal[
    "partial_pid",
    "partial_socket",
    "stale",
    "unsafe_pid",
    "unsafe_socket",
]
type ReplacementTarget = Literal["pid", "socket"]


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "aizim", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def initialized_project(tmp_path: Path) -> Path:
    root = Path(shutil.copytree(FIXTURE, tmp_path / "lean-project"))
    assert run_cli("init", str(root)).returncode == 0
    return root


def wait_for(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists() and process.poll() is None


def rpc_projection(socket_path: Path, name: str, entity_id: str) -> dict[str, JsonValue]:
    response = asyncio.run(
        rpc_call(
            socket_path,
            RpcRequest(
                operation="query_projection",
                params={"projection_name": name, "entity_id": entity_id},
                session_id=None,
            ),
        )
    )
    assert isinstance(response, RpcSuccess)
    assert type(response.result) is dict
    return response.result


def run_public_control(
    root: Path, command: PublicControlCommand
) -> subprocess.CompletedProcess[str]:
    match command:
        case "configure":
            return run_cli(
                "controller",
                "configure",
                "--project",
                str(root),
                "--provider",
                "codex",
            )
        case "register":
            return run_cli(
                "worker",
                "register",
                "--project",
                str(root),
                "--worker-id",
                "worker-1",
                "--role",
                "formalizer",
            )
        case "assign":
            return run_cli(
                "worker",
                "assign",
                "--project",
                str(root),
                "--worker-id",
                "worker-1",
                "--task",
                "prove the fixture",
            )
        case unreachable:
            assert_never(unreachable)


def bind_private_socket(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen()
    return listener


def write_live_pid(path: Path) -> None:
    path.write_text(f"{os.getpid()}\n")
    path.chmod(0o600)


def static_ownership_records(
    run_root: Path, state: StaticOwnershipState
) -> tuple[socket.socket, ...]:
    pid_path = run_root / "state.pid"
    socket_path = run_root / "state.sock"
    match state:
        case "partial_pid":
            write_live_pid(pid_path)
            return ()
        case "partial_socket":
            return (bind_private_socket(socket_path),)
        case "stale":
            pid_path.write_text("2147483647\n")
            pid_path.chmod(0o600)
            listener = bind_private_socket(socket_path)
            listener.close()
            return ()
        case "unsafe_pid":
            write_live_pid(pid_path)
            pid_path.chmod(0o644)
            return (bind_private_socket(socket_path),)
        case "unsafe_socket":
            write_live_pid(pid_path)
            listener = bind_private_socket(socket_path)
            socket_path.chmod(0o666)
            return (listener,)
        case unreachable:
            assert_never(unreachable)


def receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def replacing_rpc_server(
    pid_path: Path,
    socket_path: Path,
    target: ReplacementTarget,
) -> tuple[socket.socket, threading.Thread, list[socket.socket]]:
    write_live_pid(pid_path)
    listener = bind_private_socket(socket_path)
    replacements: list[socket.socket] = []

    def serve() -> None:
        connection, _address = listener.accept()
        with connection:
            size = int.from_bytes(receive_exact(connection, 4), "big")
            receive_exact(connection, size)
            match target:
                case "pid":
                    pid_path.unlink()
                    write_live_pid(pid_path)
                case "socket":
                    socket_path.unlink()
                    replacements.append(bind_private_socket(socket_path))
                case unreachable:
                    assert_never(unreachable)
            body = b'{"ok":true,"result":{"version":1}}'
            connection.sendall(len(body).to_bytes(4, "big") + body)

    thread = threading.Thread(target=serve)
    thread.start()
    return listener, thread, replacements
