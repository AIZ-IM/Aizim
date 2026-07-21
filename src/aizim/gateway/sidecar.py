from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

from aizim.async_lifecycle import await_cleanup

from .mcp_tools import GatewayChannel, create_gateway_server
from .transport import connect_gateway


@dataclass(frozen=True, slots=True)
class SidecarDisconnectedError(RuntimeError):
    reason: str = "GATEWAY_DISCONNECTED"

    def __str__(self) -> str:
        return self.reason


async def serve_connected_sidecar(
    channel: GatewayChannel,
    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception],
    write_stream: MemoryObjectSendStream[SessionMessage],
) -> None:
    server = create_gateway_server(channel)
    server_task = asyncio.create_task(
        server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
            raise_exceptions=False,
        )
    )
    disconnect_task = asyncio.create_task(channel.wait_disconnected())
    failure: BaseException | None = None
    try:
        done, _pending = await asyncio.wait(
            (server_task, disconnect_task), return_when=asyncio.FIRST_COMPLETED
        )
        if disconnect_task in done and server_task not in done:
            raise SidecarDisconnectedError
        await server_task
    except BaseException as error:
        failure = error
    cleanup = asyncio.create_task(_close_sidecar(server_task, disconnect_task, channel))
    try:
        interruption = await await_cleanup(cleanup)
    except BaseException as cleanup_error:
        if failure is not None:
            raise cleanup_error from failure
        raise
    if failure is not None:
        raise failure
    if interruption is not None:
        raise interruption


async def _close_sidecar(
    server_task: asyncio.Task[None],
    disconnect_task: asyncio.Task[None],
    channel: GatewayChannel,
) -> None:
    for task in (server_task, disconnect_task):
        task.cancel()
    await asyncio.gather(server_task, disconnect_task, return_exceptions=True)
    await channel.aclose()


async def _serve(socket_path: Path, session_id: str) -> None:
    channel = await connect_gateway(socket_path, session_id)
    session_id = ""
    try:
        async with stdio_server() as (read_stream, write_stream):
            await serve_connected_sidecar(channel, read_stream, write_stream)
    finally:
        await channel.aclose()


def run_gateway_sidecar(socket_path: Path, session_id: str) -> int:
    operation = _serve(socket_path, session_id)
    session_id = ""
    try:
        asyncio.run(operation)
    except Exception:
        return 6
    return 0


def scrub_session_arguments(arguments: list[str]) -> None:
    for index, value in enumerate(arguments):
        if value == "--session-id" and index + 1 < len(arguments):
            arguments[index + 1] = ""
        elif value.startswith("--session-id="):
            arguments[index] = "--session-id="


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aizim-gateway-sidecar")
    parser.add_argument("--broker-socket", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    process_argv = sys.argv if argv is None else None
    command_line = list(sys.argv[1:] if argv is None else argv)
    argv = None
    arguments = parser.parse_args(command_line)
    session_ids = [arguments.session_id]
    arguments.session_id = ""
    scrub_session_arguments(command_line)
    if process_argv is not None:
        scrub_session_arguments(process_argv)
    return run_gateway_sidecar(arguments.broker_socket, session_ids.pop())
