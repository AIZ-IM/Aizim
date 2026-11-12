from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

from mcp import ClientSession
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_client_server_memory_streams


@asynccontextmanager
async def create_connected_server_and_client_session(
    server: Server,
) -> AsyncGenerator[ClientSession]:
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        task = asyncio.create_task(
            server.run(*server_streams, server.create_initialization_options())
        )
        try:
            async with ClientSession(*client_streams) as client:
                await client.initialize()
                yield client
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
