from __future__ import annotations

import asyncio


async def await_cleanup(task: asyncio.Task[None]) -> asyncio.CancelledError | None:
    interruption: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError as error:
            if interruption is None:
                interruption = error
    task.result()
    return interruption
