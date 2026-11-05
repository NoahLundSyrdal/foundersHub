"""In-process pub/sub feeding the SSE stream (/api/events). Notifications only; SQLite is the truth."""

import asyncio
import json

_subscribers: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}


def publish(event: str, data: dict) -> None:
    """Safe from any thread (sync routes and tools run in worker threads)."""
    msg = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
    for q, loop in list(_subscribers.items()):
        loop.call_soon_threadsafe(q.put_nowait, msg)


async def subscribe():
    q: asyncio.Queue = asyncio.Queue()
    _subscribers[q] = asyncio.get_running_loop()
    try:
        yield "event: hello\ndata: {}\n\n"
        while True:
            try:
                yield await asyncio.wait_for(q.get(), timeout=15)
            except TimeoutError:
                yield ": keepalive\n\n"
    finally:
        _subscribers.pop(q, None)
