from __future__ import annotations

import asyncio

from aizim.gateway import GatewaySessionBroker
from aizim.gateway.socket_alias import ProjectSocketAlias
from aizim.lean import SharedLeanRuntime

from .promotion_consumer import PromotionConsumer


async def cleanup_run(
    failure_task: asyncio.Task[None] | None,
    consumer_task: asyncio.Task[None] | None,
    stop: asyncio.Event,
    consumer: PromotionConsumer,
    sessions: GatewaySessionBroker,
    runtime: SharedLeanRuntime,
    alias: ProjectSocketAlias,
) -> tuple[BaseException, ...]:
    errors: list[BaseException] = []
    if failure_task is not None:
        failure_task.cancel()
        await asyncio.gather(failure_task, return_exceptions=True)
    stop.set()
    consumer.notify_submission()
    await asyncio.sleep(0)
    if consumer_task is not None:
        if not consumer_task.done():
            consumer_task.cancel()
        result = (await asyncio.gather(consumer_task, return_exceptions=True))[0]
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            errors.append(result)
    for operation in (sessions.aclose, runtime.aclose):
        try:
            await operation()
        except BaseException as error:
            errors.append(error)
    try:
        alias.close()
    except BaseException as error:
        errors.append(error)
    return tuple(errors)
