"""In-process pub/sub feeding the SSE stream (/api/events). Notifications only; SQLite is the truth."""

import asyncio
import json

_subscribers: set[asyncio.Queue] = set()


def publish(event: str, data: dict) -> None:
    msg = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
    for q in list(_subscribers):
        q.put_nowait(msg)


async def subscribe():
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.add(q)
    try:
        yield "event: hello\ndata: {}\n\n"
        while True:
            try:
                yield await asyncio.wait_for(q.get(), timeout=15)
            except TimeoutError:
                yield ": keepalive\n\n"
    finally:
        _subscribers.discard(q)
