"""Await listener worker completion even when its asynchronous owner is cancelled."""
import asyncio


async def listener_call(function, *args, **kwargs):
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Cancelling to_thread only cancels its waiter, not the running invocation.
        # Complete that invocation before the owning attachment may finish closing.
        await worker
        raise
